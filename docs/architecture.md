# Архитектура control-plane

## Слои

```
api/            HTTP-контракт: валидация, auth-dependency, маппинг ошибок, сериализация
application/    Команды и запросы (бизнес-логика), авторизация, запись событий
domain/         Enum'ы и доменные ошибки (без инфраструктурных зависимостей)
infrastructure/ PostgreSQL (модели, engine), auth (API-ключи), idempotency, realtime
worker/         Background-процесс: outbox, sweep аренды, GC идемпотентности
```

Route handler делает ровно четыре вещи: валидирует HTTP-контракт, получает
`AuthContext` из credentials, вызывает команду/запрос application-слоя,
превращает результат в HTTP-ответ. Бизнес-правил в handler'ах нет; команды
повторно проверяют permissions сами (defense in depth).

## Концептуальная модель (v0.2 Organization Model)

```
Tenant
 └── Workspace (иерархия: adjacency list, slug уникален среди siblings)
      ├── Principals (human | agent | service — единый протокол координации)
      │    ├── Roles         (кто ты в организации; scope = tenant или поддерево workspace)
      │    ├── Capabilities  (что ты способен делать; metadata JSONB)
      │    └── Skills        (какие исполняемые интерфейсы доступны; registry-метаданные)
      │
      └── Tasks (+ requirements: roles/capabilities/skills — все обязательны)
           ├── Relations (parent | blocks | depends_on | spawned_by | related_to)
           ├── Claim (lease + fencing token)
           │    └── Run (попытка исполнения; фиксирует token на старте)
           │         ├── Control Messages (durable queue/steer/redirect/cancel)
           │         └── Artifacts (append-only ссылки на результаты)
           │
           └── Approvals (одна запись = одно решение; principal или требуемая роль)
```

Ключевое различие сущностей исполнения:

```
Principal = identity            (кто; человек и агент равноправны)
Session   = live-контекст       (живое подключение/исполнение, lease + heartbeat;
                                 v0.3: несёт регистрацию harness — ADR-0015)
Claim     = exclusive ownership (арендованное владение задачей, fencing token)
Run       = execution attempt   (конкретная попытка; результат, output, артефакты;
                                 v0.3: + suspended, cancel-request, бюджет)
```

Continuity агента живёт НЕ в LLM-разговоре, а в связке
`principal + workspace + task + run + checkpoints + artifacts + events`.
Control Plane не исполняет LLM/agent-логику — только координирует.

## Harness Protocol (v0.3)

```
                 PRODUCT / WHITE LABEL
                ┌──────────────────┐
                │  Control Plane   │   Organization / Work / Execution / Audit
                └────────┬─────────┘
                         │  control-harness/2 (REST + event journal)
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
 Codex / Claude    control-plane-agent    CLI
  (MCP, human)      (autonomous)      (debugging)
```

Harness — недоверенный клиент; регистрация — блок `harness` при открытии
сессии (negotiation версии протокола, декларация protocol capabilities).
Bootstrap — `GET /harness/context`; discovery — `GET /work/available`
(advisory; авторитетен только claim); операционный контекст исполнения —
`GET /runs/{id}/context`. Ожидание approval — gate + `:suspend` (ADR-0018);
восстановление — recovery protocol (docs/harness-protocol.md §12);
execution audit — `run_actions` (ADR-0019). Human и autonomous harness
используют одни и те же endpoint'ы — различие только в политике клиента.

Active Turn Control хранит управляющие intent в PostgreSQL, а не в памяти
harness. `run_control_messages` имеют Run-local seq и lifecycle
`accepted → applied|rejected|superseded`; acknowledgement проверяет holder,
Claim, fencing и optimistic versions. Event stream будит harness, но
авторитетен read model таблицы/Run Context. `force_cancel` в одной транзакции
terminalizes Run, освобождает Claim и каскадирует по `spawned_by` child Runs
(независимо от cancellation policy handle, HRS-7);
process kill остаётся обязанностью execution runtime.

Claim задачи разрешён при одновременном выполнении четырёх условий:
`API authorization (tasks.claim)` ∧ `organizational eligibility (требования)`
∧ `task readiness (зависимости done)` ∧ `concurrency-правила (лок, лизы,
fencing)`. Роли/capabilities/skills никогда не заменяют API permissions
(ADR-0009, ADR-0010).

### Human Operator Harness (v0.6)

Codex и Claude Code запускают один `control-plane-mcp` с разными
process-configured harness metadata. Сервер выводит `controlLevel` из
Principal kind; поле наблюдаемое и не участвует в authorization. Handoff —
одна транзакция `checkpoint + suspend Run + release Claim + task todo +
events/outbox`. Следующий harness всегда создаёт новый Claim/Run и читает
историю через Run Context. Operator repository хранит инструкции и
несекретную connection config, а target repositories — код; ни один из них не
заменяет PostgreSQL как источник coordination state (ADR-0042).

### Effective Harness Manifest (v0.7)

Каждый Run получает immutable снимок effective runtime configuration
(ADR-0043). `base` + `provenance` — frozen и хешируются в `baseHash`:
identity, project policy с её слоями, tool policy с объяснением видимости
каждого tool, budgets, заявленные worker profile / execution backend / model,
redaction policy. `captured` держит движущееся состояние — operational cursor
и **ссылку** на Memory Context Pack — и в hash не входит, иначе растущий
курсор сделал бы воспроизводимость недостижимой.

Манифест компилируется автоматически в транзакции старта Run; повторная
компиляция создаёт новую версию только при изменении `baseHash`. Provider
fallback обязан объявить больший `model.attempt` и потому всегда порождает
новую recorded attempt. Ephemeral steering/warnings живут отдельными
append-only строками и не меняют frozen base.

### Scoped Tool Discovery (v0.7)

Каталог инструментов разделён на четыре слоя (ADR-0045): **Capability
Catalog** (`skills` тенанта) — что runtime технически умеет; **Effective Tool
Policy** — что разрешено этому Principal, Run и workspace сейчас; **Tool
Discovery View** (`GET /tools`) — bounded проекция их пересечения; **Action
Authorization** — пересчёт решения в момент записи действия.

Решение принимает одна чистая функция (`domain/tool_discovery.py`), общая для
манифеста, discovery и invocation: разошедшиеся реализации означали бы, что
поиск и gate расходятся молча. Ревизии каталога и политики выводятся хешем
(общий канонический формат — `domain/canonical.py`), поэтому любое изменение
registry, назначений или governance инвалидирует кэшированную проекцию через
`viewHash`/`ETag`.

Инструмент вне effective policy нельзя ни найти, ни описать (`404
tool_not_found`, тот же ответ, что и для несуществующего), ни исполнить (`403
tool_not_authorized`). Заявленные сессией `skills.protocol.*` влияют на
видимость, но **не** на право исполнения: это утверждение клиента о себе, а не
полномочие.

## Context Memory (v0.4, опционально)

```
                  ┌─────────────────┐
                  │  Control Plane  │  operational truth · event journal
                  └────────┬────────┘
                           │ reliable (tx_id, sequence) cursor
                           ▼
                  ┌─────────────────┐
                  │ Context Adapter │  event → Observation · durable cursor
                  └────────┬────────┘  at-least-once · poison parking
                           │ HTTP (единственная runtime-зависимость)
                           ▼
                  ┌─────────────────┐
                  │ Context Memory  │  независимый продукт: retrieval,
                  │     Engine      │  temporal facts, Context Compiler
                  └────────┬────────┘
                           │ ContextPack (+ ephemeral current state)
                           ▼
                     Harness / Human / AI
```

Граница жёсткая: только HTTP-контракт (ADR-0025); Memory optional и не
участвует в readiness; доставка наблюдений — at-least-once с дедупликацией
по стабильной identity (ADR-0026); явный «remember» — replayable-событие
журнала (ADR-0027); working context строго разделяет authoritative current
state и durable memory (ADR-0028).

### Доставка per-tenant (v0.5)

Курсор доставки стал per-tenant: ключ `event_consumer_cursors` —
`(name, tenant_id)`, а parked-состояние, счётчик отказов и backoff — колонки
той же строки. Цикл выбирает Tenant-ов с непустым отставанием, у которых
истёк backoff, в порядке давности обслуживания (round-robin), и обрабатывает
не более `CP_CONTEXT_MAX_TENANTS_PER_CYCLE` за цикл по
`CP_CONTEXT_TENANT_BATCH_SIZE` событий. Отказ провайдера паркует **только
свою** строку — остальные Tenant продолжают течь (ADR-0036).

Курсор по-прежнему нельзя продвинуть вперёд ничем, кроме подтверждённой
доставки: операторский `:redrive` снимает parked-состояние и повторяет ту же
позицию, `:rebuild` двигает строго назад (ADR-0037).

### Retention журнала (v0.5)

`:archive` переносит подтверждённые и достаточно старые события в
`event_archive` и поднимает `event_journal_floor`; replay прозрачно
охватывает архив, поэтому аудит и воспроизводимость не меняются. Горизонт
ограничен одновременно минимальной позицией по всем consumer-курсорам и
самым старым недоставленным outbox-событием — то, что кому-то ещё нужно, не
уедет. `:prune` физически удаляет из архива и поднимает `archive_floor`;
запрос курсора ниже него — машиночитаемая ошибка `cursor_below_journal_floor`,
а не тихий пропуск (ADR-0038).

## Project Model (v0.5)

Одно дерево, а не два. `Workspace` остаётся единственной иерархией; `Project` —
это `project_profiles`, привязанный к Workspace отношением один-к-одному
(`UNIQUE (workspace_id)`). Поля `parent_project_id` нет: родитель проекта
вычисляется рекурсивным CTE как ближайший предок Workspace с профилем
(ADR-0031, ADR-0035).

```text
Tenant
└── Workspace (type=portfolio)
    ├── Workspace (type=project)  + Project Profile      <- проект A
    │   ├── Workspace (type=workstream)                  <- принадлежит A
    │   └── Workspace (type=project) + Project Profile   <- проект B, новый scope
    └── Workspace (type=team)
```

Что из этого следует и как это устроено:

- **Workspace Types** — tenant-scoped справочник (`key`, `field_schema`,
  `allowed_child_types`). Правило родитель/ребёнок проверяется при
  create/move/retype под тем же per-tenant advisory lock, что и остальные
  структурные мутации, поэтому «оба ребёнка прошли проверку» невозможно.
  Системный тип `generic` есть у каждого Tenant и разрешает любых детей —
  это то, что делает миграцию v0.4 → v0.5 бесшовной (ADR-0029).
- **Project Templates** версионированы и неизменяемы: триггер БД разрешает
  единственную мутацию `active → deprecated`. Проект ссылается на точную
  версию, поэтому его `custom_fields` и `status_key` всегда проверяются по той
  схеме, против которой были записаны (ADR-0030).
- **Lifecycle** пользовательский, решения системные: каждый статус шаблона
  отображается в одну из пяти категорий (`planned`, `active`, `paused`,
  `terminal_success`, `terminal_cancelled`), категория денормализована на
  профиле, и весь core ветвится только по ней.
- **Task Types** (v0.8, ADR-0048) — тот же приём для work item: tenant-scoped
  версионируемый реестр с `field_schema` и `lifecycle_schema`, иммутабельный по
  тому же триггерному правилу. Статус задачи — пара «пользовательский ключ +
  системная категория» (`backlog`, `active`, `blocked`, `terminal_success`,
  `terminal_cancelled`); claimability, готовность зависимостей и завершение
  считаются только по категории. Системный тип `task` есть у каждого Tenant и
  несёт шесть прежних статусов — это то, что делает миграцию v0.7 → v0.8
  бесшовной. `field_schema` типа применяется к `custom_fields` задачи
  (ADR-0049) — по той версии, которую задача закрепила; плановые даты
  `start_date` и `due_date` вынесены в типизированные колонки, потому что по
  ним нужны индексы, фильтры и порядок чтения «ближайшие первыми».
- **Конфигурация** — append-only `project_config_revisions`. Создание ревизии
  не активирует её; активация — отдельная команда под row lock профиля с
  обязательным `If-Match`. Авторитетный указатель ровно один:
  `project_profiles.active_config_revision_id` (ADR-0032).
- **Effective config** складывается детерминированно: template defaults →
  разрешённые settings предков (от корня к родителю) → активная ревизия →
  overlay профиля. `settings`/`memory` сливаются рекурсивно (массивы и скаляры
  заменяются целиком), `views` заменяются нацело, `governance` сворачивается
  операцией «строже». Ответ несёт provenance по каждому верхнеуровневому ключу.
- **Governance** — фиксированный типизированный словарь с объявленным
  частичным порядком (упорядоченный enum, «строгий true», подмножество,
  числовой потолок где `null` = без границы). Потомок может только ужесточить;
  попытка ослабления отклоняется до commit, а `:move` перепроверяет всё
  перемещаемое поддерево и откатывается целиком (ADR-0033).
- **Принадлежность задачи проекту не хранится**: `TaskOut.projectId`
  резолвится из дерева одним batch-CTE на страницу. `projectId`-фильтр
  разворачивается в множество `workspace_id` и попадает в `WHERE` до
  пагинации, поэтому страницы не пропускают и не дублируют записи (ADR-0035).

## Транзакционная модель

Каждая мутирующая команда исполняется в одной транзакции
(`infrastructure/db/engine.py::transaction`). Внутри неё:

1. состояние меняется в нормализованных таблицах;
2. `record_event()` добавляет строку в `events` (монотонный `sequence`)
   и строку в `outbox`;
3. выполняется `pg_notify('cp_events', ...)` — PostgreSQL доставит уведомление
   слушателям **только при commit**, поэтому подписчики никогда не будят по
   откаченным данным;
4. при использовании `Idempotency-Key` сохранённый ответ пишется той же
   транзакцией.

Commit атомарно публикует всё; rollback не оставляет ничего (проверяется
тестом transactional audit).

Важное следствие дизайна «без ORM-relationships»: SQLAlchemy unit of work не
выводит порядок INSERT между мапперами из FK — там, где в одной транзакции
создаётся несколько зависимых сущностей, порядок задаётся явными `flush()`.

## Конкурентность и fencing

Инварианты держит PostgreSQL, а не Python:

| Механизм | Что защищает |
|---|---|
| `SELECT ... FOR UPDATE` строки задачи | Критическая секция claim/update/complete |
| Частичный уникальный индекс `task_claims(task_id) WHERE status='active'` | ≤ 1 активного claim на задачу даже при регрессии кода |
| `tasks.version` + `If-Match` | Optimistic concurrency (lost update) |
| `tasks.claim_epoch` = fencing token | Отсечение проснувшихся старых сессий |
| Уникальный `(tenant_id, public_id)` + upsert-счётчик `task_counters` | Генерация `TASK-000001` без коллизий |
| PK `(tenant_id, key)` + `INSERT ... ON CONFLICT DO NOTHING` | Idempotency: ровно один исполнитель |
| Частичный уникальный индекс `runs(task_id) WHERE status='running'` | ≤ 1 запущенного run на задачу |
| Advisory lock per-tenant (`cp:ws:*`, `cp:taskgraph:*`) + recursive CTE | Ацикличность дерева workspaces и графа зависимостей под гонками |
| Составные FK `(tenant_id, task_id)` в `task_relations` | Cross-tenant рёбра невозможны на уровне БД |
| `FOR UPDATE SKIP LOCKED` | Конкурентные worker'ы не дерутся за outbox/sweep |

### Алгоритм claim

Захват описан в `application/commands/claims.py::_claim_locked_task` и в точности
следует спецификации: лок строки задачи → проверка существующего claim (живой →
`409 task_already_claimed`; истёкший → `stale` + событие `claim.expired`) →
`claim_epoch += 1` → новый claim с `fencing_token = claim_epoch` → указатель
`active_claim_id` → перевод задачи в `claimStatus` её типа, если ребро объявлено
(v0.8, ADR-0048) → `version += 1` → событие `task.claimed` + outbox → commit.

Просроченный claim реквизируется этой же командой атомарно — корректность не
зависит от воркера. Worker лишь ускоряет конвергенцию (помечает stale фоном).

### Порядок блокировок (anti-deadlock)

Глобальная дисциплина: **session → task → claim → run**. Захват claim'а берёт
share-lock сессии до лока задачи (гонка с `close_session` не может оставить
активный claim на закрытой сессии); `close_session` и sweep сессий воркером
идут в том же порядке. Там, где строка была прочитана до взятия лока,
повторный `FOR UPDATE`-select выполняется с `populate_existing=True` — иначе
SQLAlchemy вернул бы закешированный в identity map пре-лок снимок и
«перепроверка после лока» была бы фиктивной.

### Структурные локи дерева (v0.5)

Порядок для дерева: **per-tenant advisory tree lock → workspace row →
project row**. Его берут create/move/retype Workspace, создание Workspace
Type и создание Project. Аллокация версий Project Template использует
отдельный advisory lock по ключу `cp:tpl:<tenant>:<key>` и никогда не берётся
внутри tree lock, поэтому цикла между ними нет.

Там, где уникальность всё же обеспечивает индекс (профиль на Workspace, ключ
типа, внешняя ссылка), вставка выполняется внутри `SAVEPOINT`: проигравший
гонку получает честный `409`, а не отравленную транзакцию.

### Gate мутаций при живом claim

Claim считается **живым**, только если он `active`, не истёк И его сессия
активна и не истекла. Пока у задачи есть живой claim, PATCH/`:complete`
обязаны предъявить `claimId` + `fencingToken`; проверяются: совпадение с
`active_claim_id`, равенство token == `claim_epoch` == `fencing_token`
claim'а и принадлежность claim вызывающему principal. Любое несовпадение —
`409 stale_claim` / `409 task_claimed` / `403 claim_holder_mismatch`.
Claim с мёртвой сессией не блокирует задачу: его пожнёт следующий захват
(reason `session_inactive`) или sweep воркера.

## Lease-модель

Сессии и claims — аренды с `expires_at`, продлеваемые heartbeat'ами. Истечение
обрабатывается тремя независимыми путями (any-of, корректность не зависит от
каждого в отдельности):

1. **лениво** — команда, встретившая истёкшую аренду, отклоняет операцию
   (и heartbeat сессии фиксирует переход в `stale` коммитом, а не исключением);
2. **при захвате** — claim реквизирует истёкший активный claim атомарно;
3. **фоном** — worker переводит истёкшие sessions/claims в `stale` и пишет
   события `session.expired` / `claim.expired`.

## Realtime

WebSocket — не источник истины, а сигнал «проснись и дочитай»:

```
команда ──txn──▶ events + outbox + pg_notify ──commit──▶ LISTEN-коннект хаба
                                                              │ wake(tenant)
клиент ◀── send(json) ── fetch_events_after(position) ◀───────┘  (+ poll fallback)
```

Хаб держит один выделенный LISTEN-коннект на процесс и будит подписчиков
тенанта. Каждое WS-соединение хранит позицию последнего отданного события,
читает события из таблицы и при таймауте (`ws_poll_interval_seconds`)
опрашивает её самостоятельно — потерянный NOTIFY не приводит к потере
событий. Reconnect с `?after=<opaque cursor>` дочитывает пропущенное. Журнал
append-only (триггеры в БД запрещают UPDATE/DELETE/TRUNCATE).

**Надёжный replay-курсор (v0.4).** Порядок доставки — пара
`(tx_id, sequence)` под фильтром «стабильного горизонта»
`tx_id < pg_snapshot_xmin(pg_current_snapshot())`. `tx_id` — 64-битный xid8
пишущей транзакции (server default, монотонный, без wrap-around); всё, что
ниже горизонта, уже завершилось: закоммиченные строки видимы, откатившиеся
строк не оставили. Любая незавершённая транзакция имеет `tx_id >= xmin`, то
есть её события сортируются строго ПОСЛЕ каждой позиции, которую читатель мог
получить, — курсор, продвигающийся только по выданным позициям, физически не
может перешагнуть событие, которое закоммитится позже. Плата за гарантию —
задержка выдачи на время самой долгой открытой пишущей транзакции (наши
команды короткие; доставка задерживается, но не теряется).

`sequence` (присваивается при INSERT) остаётся идентификатором события,
audit-полем и детерминированным порядком внутри транзакции — но НЕ является
replay-курсором: порядок sequence и порядок xid у конкурентных команд могут
инвертироваться (v0.3-дефект, воспроизводится regression-тестом
`test_event_prefix_is_complete_under_xid_inversion`, до v0.4 — строгий
xfail). Публичный курсор непрозрачный и версионированный (`ec1_<base64url>`,
см. `application/event_cursor.py`); legacy `?after=<sequence>` и старый
формат `nextCursor` принимаются и адаптируются сервером (при переключении
возможна повторная выдача уже виденных событий — at-least-once). Индекс
`(tenant_id, tx_id, sequence)` обслуживает пагинацию по row-comparison.

## Worker

`python -m control_plane.worker` крутит цикл: outbox (батчи по
`FOR UPDATE SKIP LOCKED`, bounded retry с экспоненциальным backoff, после
исчерпания попыток запись остаётся с `last_error`) → sweep сессий → sweep
claims → GC идемпотентности. Каждая под-задача — отдельная транзакция; падение
процесса откатывает текущий батч, и записи обрабатываются снова (at-least-once).
Graceful shutdown по SIGTERM/SIGINT.

## Наблюдаемость

JSON-логи (stdout) с `request_id` из contextvar; `X-Request-ID` принимается от
клиента (после санитизации) или генерируется; идентификатор возвращается в
заголовке и в каждом error envelope. Значения Authorization/ключей в логи не
попадают (redaction), ключи упоминаются только по префиксу. `/health/live`,
`/health/ready` (доступность БД + актуальность миграций), `/metrics`
(Prometheus text). Стектрейсы клиенту не возвращаются.
