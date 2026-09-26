# Control Plane v0.5 — подтверждённый baseline

Статус: завершено и принято как исходная точка следующих этапов.

Версия: `v0.5 — Project Model & Reliability Backlog`
Baseline commit: вершина `main` после серии v0.5 (последний коммит —
этот документ; серия перечислена ниже)
Предыдущий baseline: `78e9a48` (`v0.4 — Reliable Event Cursor, Context Memory
Integration & White-Label Core`)

## Для кого этот документ

Для инженера, который планирует v0.6 или интегрирует Control Plane с другими
компонентами. После чтения он должен отличать реализованные гарантии от
backlog и не проектировать заново уже закрытое.

## Результат версии

v0.5 закрыла два направления одновременно:

1. **Project Model** поверх существующего дерева Workspace — портфели,
   программы, проекты, подпроекты и рабочие потоки без второго дерева и без
   второго поля принадлежности.
2. **Обязательный reliability backlog v0.4** — per-tenant изоляция доставки,
   операторский redrive, retention/archive/rebuild журнала, сквозной
   `X-Run-Id` и закрытие окна совместимости продуктового имени.

Реализация выполнена одиннадцатью атомарными коммитами после v0.4. Рабочее дерево
baseline чистое.

## Project Model

Ключевое решение: **проектная иерархия выводится, а не хранится**.

| Механизм | Гарантия |
|---|---|
| Workspace Types (ADR-0029) | tenant-scoped `key` + `field_schema` + `allowed_child_types`; правило проверяется при create/move/retype под per-tenant advisory lock; системный тип `generic` делает миграцию v0.4 бесшовной |
| Project Templates (ADR-0030) | версия неизменяема с момента записи (триггер БД); единственная разрешённая мутация — `active → deprecated`; проект ссылается на точную версию |
| Project Profile (ADR-0031) | `UNIQUE (workspace_id)`; составные FK привязывают профиль к Workspace и Template того же Tenant; archived Workspace не принимает профиль; `project.archive` не трогает Workspace, задачи, события и историю конфигурации |
| Lifecycle (ADR-0031) | пользовательские статусы → пять системных категорий; core ветвится только по категории; переход требует `If-Match` и объявленного ребра; отдельное событие `project.status_changed` |
| Config revisions (ADR-0032) | append-only (триггер: меняется только `activated_at`); создание не активирует; активация — отдельная команда под row lock; ровно один авторитетный указатель |
| Effective config (ADR-0032) | слои template → ancestors → revision → profile; объекты сливаются рекурсивно, массивы и скаляры заменяются, `views` заменяются нацело; ответ несёт provenance по каждому верхнеуровневому ключу |
| Governance (ADR-0033) | фиксированный типизированный словарь; `stricter` — настоящий meet (множества пересекаются); потомок может только ужесточить; `:move` перепроверяет всё поддерево и отклоняется целиком |
| External references (ADR-0034) | `UNIQUE (tenant, system, type, id)`; identity-колонки неизменяемы (триггер); mapping, а не источник истины |
| Project scope (ADR-0035) | `TaskOut.projectId` резолвится из дерева batch-CTE; `projectId`-фильтр разворачивается в множество `workspace_id` **до** пагинации; archived проект не выдаёт новую работу, не трогая живые claim и run |

Секреты не принимаются ни в config, ни в `views`, ни в metadata внешних
ссылок, ни в settings профиля: запись отклоняется `422
secret_material_rejected`, допустим только opaque `secretRef`.

## Reliability backlog v0.4 — закрыт

| Пункт backlog | Что сделано |
|---|---|
| Операторский redrive parked event | `GET /operations/context-adapter`, `:redrive`, `:rebuild`; ни одна операция не может продвинуть курсор вперёд, то есть пропустить событие; каждая пишет audit-событие; идемпотентно (ADR-0037) |
| Per-tenant delivery isolation/quotas | ключ курсора `(name, tenant_id)`; parked-состояние и backoff per-tenant; round-robin по давности обслуживания; `CP_CONTEXT_TENANT_BATCH_SIZE` и `CP_CONTEXT_MAX_TENANTS_PER_CYCLE` (ADR-0036) |
| Сквозной `X-Run-Id` | валидируется по формату, эхом возвращается, пишется в `events.trace_run_id`, в outbox, в `data.traceRunId` наблюдения, уходит заголовком в Memory и в логи как `run_id` (ADR-0039) |
| Retention/archive/rebuild | `event_archive` + per-tenant floor; горизонт ограничен минимумом consumer-курсоров И недоставленным outbox; replay прозрачно охватывает архив; курсор ниже floor — `422 cursor_below_journal_floor` (ADR-0038) |
| Удаление legacy naming inputs | пакет-шим, алиасы классов, три console-script, шесть env-переменных, три storage-локации и `protocol.legacyNames` удалены; устаревшая настройка даёт явную ошибку миграции (ADR-0040) |
| OpenCode adapter | `control-plane-opencode` поверх подтверждённого HTTP-контракта `opencode serve`; continuity через Run Checkpoints; contract-тесты против стенда (ADR-0041) |

Миграция курсора replay-safe: глобальная позиция G становится той же позицией
для каждого Tenant, поэтому переигрывания всего журнала не происходит.

## Подтверждённые quality gates

Прогон на baseline-коммите:

| Проверка | Результат |
|---|---|
| `uv sync` | 65 пакетов, чисто |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 241 files already formatted |
| `uv run mypy src` | no issues found in 108 source files |
| `uv run pytest` | **457 passed, 13 skipped** |
| Контрактные тесты с живым Memory Service | **21 passed, 1 skipped** (`CP_TEST_MEMORY_URL`) |
| `docker compose build` | api / worker / context-adapter собраны |
| Docker Compose startup + readiness | 4 сервиса Up, `{"status":"ready","revision":"1adf50721f1e"}` |
| Full-system E2E (`scripts/e2e_v05.py`) | **44 проверки, все PASS** |

Разбивка pytest по пакетам: unit 135, integration 253, concurrency 26,
client 25, contract 22, e2e 9.

Пропущенные 13 — контрактные тесты, требующие внешних сервисов: 12 Memory
(запускаются с `CP_TEST_MEMORY_URL`, прогнаны отдельно и зелёные) и 1 живой
`opencode serve` (бинарь не входит в образ, см. ограничения).

### Migration matrix

| Шаг | Результат |
|---|---|
| fresh → head | 5 ревизий применены |
| head → v0.4 (`2cb05920015d`) | 1 downgrade |
| v0.4 → head | 1 upgrade |
| head → base | 5 downgrade |
| base → head | 5 upgrade |
| `alembic check` | No new upgrade operations detected |

На наполненной базе roundtrip проверен тестами
`tests/integration/test_migration_v05.py`: журнал не переписывается ни в одну
сторону, workspace'ы backfill'ятся системным типом, per-tenant курсоры
получают ту же позицию, что была у глобального.

### Concurrency suite (26 тестов, из них 5 новых для v0.5)

| Сценарий | Результат |
|---|---|
| 20 конкурентных создания Project Profile на одном Workspace | 1 × 201, 19 × 409 `project_exists`, 1 профиль, 1 событие `project.created` |
| Конкурентная активация двух config revisions с одной expected version | 1 × 200, 1 × 409 `version_conflict`; ровно одна ревизия помечена активированной |
| Конкурентные move + activate | итоговая иерархия всегда внутри ceiling, частично применённых изменений нет |
| Конкурентное создание Workspace Type | 1 успех, остальные 409 |
| Конкурентная аллокация версий Template | версии 1..6 без пропусков и дублей |
| Poison Tenant A / доставка Tenant B | B продолжает доставку, A паркуется, после redrive продолжает с той же позиции |
| Crash ambiguity адаптера | повтор дедуплицирован, второго логического наблюдения нет |

## Производительность

Измерено `scripts/bench_v05.py` на локальном Docker-стенде (PostgreSQL 16,
дерево 10 003 Workspace, backlog 10 000 задач, 4 Tenant).

| Контур | v0.4 baseline | v0.5 | Комментарий |
|---|---|---|---|
| Discovery без фильтра | ~24 мс p50 | 37 мс p50 | тот же путь; разница — другая машина/нагрузка, не регрессия кода |
| Discovery по workspace subtree | — | 39 мс p50 | |
| Discovery `projectId` (точный scope) | — | **39 мс p50** | +0…2 мс к workspace-фильтру |
| Discovery `projectId&includeSubprojects` | — | 39 мс p50 | |
| `GET /tasks?projectId` | — | 4 мс p50 | |
| Резолюция проекта для страницы задач | — | 4 мс (50) / 5 мс (200) | один batch-CTE на страницу |
| `GET /workspaces/tree` полное, 10 003 узла | — | 1.6 с wall-clock, **~100–135 мс серверная часть** | ответ 2.6 МБ; остальное — передача и разбор на клиенте |
| `GET /workspaces/tree?depth=1` | — | 4 мс | рекомендуемый режим |
| Effective config, цепочка глубины 16 | — | 3 мс p50 | |
| Adapter catch-up, 8 071 событие / 4 Tenant | ~219 obs/с (v0.4, 1 Tenant) | догнал < 1 с включая рестарт | верхняя оценка времени, не чистая скорость |

`EXPLAIN ANALYZE`: рекурсивный CTE дерева 4–6 мс, scope-фильтр discovery
2.3–2.6 мс, seed-скан резолюции 0.05–0.07 мс. **Новых индексов не добавлено**
— измерение не подтвердило пользу. Числа характеризуют измеренный baseline и
не являются SLA.

## Исправления adversarial review

Шесть независимых линз по диффу v0.4→v0.5 дали 32 кандидата; после
адверсариальной верификации (три скептика на находку, большинство
опровержений убивает находку) выжили 12. Все они, а также кандидаты,
исправленные до окончания верификации, устранены:

- retention работала глобально, но авторизовалась правами одного Tenant —
  теперь floor, горизонт и DELETE привязаны к Tenant вызывающего;
- Context Adapter читал только горячую таблицу, из-за чего `:rebuild` ниже
  journal floor пропускал весь архив;
- `current_position` игнорировал архив, из-за чего свежий follower получал
  origin после архивации;
- `GET /events` без курсора становился вечным 422 после первого prune;
- legacy sequence-курсор ниже floor не проверялся;
- страница на границе архив/горячая таблица могла разъехаться с конкурентной
  архивацией (теперь перечитывает floor и один раз повторяет);
- незавершённый батч адаптера мог затереть операторский rebuild;
- `stricter` не был meet для множеств (несравнимые множества расширяли
  результат);
- ревизия молча снимала `lockedSettings`, объявленные шаблоном;
- предок без `views` обнулял собственные представления потомка;
- provenance приписывал значение промежуточному предку вместо исходного;
- `governance` внутри `settings` принимался вопреки ADR-0033;
- `:move` не перепроверял `lockedSettings`;
- сохранённая JSON Schema могла содержать внешний `$ref` (SSRF через
  резолвер) и невычислимая схема давала 500;
- `views` не проверялись на секреты, metadata не ограничивалась до
  рекурсивного обхода;
- `archive_workspace_type` работал без tree lock;
- `GET /projects` делал N+1 и молча игнорировал неполный external-фильтр;
- PATCH молча терял `templateVersion` без ссылки на шаблон;
- ключ ровно с `operations.manage` получал 403 ПОСЛЕ мутации, и redrive
  откатывался — теперь диагностика принимает и read, и manage;
- ответ диагностики имел две разные формы (до и после появления строки
  курсора) — теперь форма одна.

Каждое исправление закрыто регрессионным тестом.

## Принятые ограничения

- `GET /workspaces/tree` без `depth` на дереве в 10 000 узлов — документ
  ~2.6 МБ; серверная часть ~100 мс, но передача и разбор на клиенте
  доминируют. Используйте `depth`.
- Ужесточение `allowed_child_types` или `field_schema` типа не переваливает
  существующее дерево в невалидное ретроактивно: правило проверяется только
  при мутациях.
- Provenance даётся по верхнеуровневым ключам разделов, не по каждому листу.
- Удалить унаследованный вложенный ключ можно только переопределив
  объект-контейнер целиком: sentinel-удаления нет.
- External reference нельзя удалить через API (только создать и обновить
  metadata).
- `governance` задаётся только через config revisions.
- Context Adapter остаётся одним процессом: per-tenant изоляция изолирует
  **отказы**, а не даёт параллелизм.
- Архив журнала живёт в той же базе — `:archive` уменьшает горячую таблицу и
  стоимость её индексов, но не общий размер тома. Внешнее объектное хранилище
  — следующий этап.
- Retention — операторская команда, планировщика нет.
- Dead-letter запись outbox удерживает горизонт retention, пока её не
  разберут.
- Индексы миграции строятся не `CONCURRENTLY`; backfill `workspaces.type_id`
  трогает каждую строку — нужен maintenance window.
- `/metrics` не аутентифицирован и защищается deployment boundary;
  `context_adapter_*` агрегированы по Tenant намеренно (кардинальность).
- OpenCode adapter проверяется контрактным стендом, повторяющим
  документированные пути; живой `opencode serve` в образ не входит и
  включается `CP_TEST_OPENCODE_URL`.
- Выдача событий по-прежнему задерживается самой долгой открытой пишущей
  транзакцией **кластера** (`pg_snapshot_xmin` — cluster-wide): не запускайте
  полный тестовый прогон на PostgreSQL, который параллельно пишет из другой
  базы.
- Клиенты v0.3/v0.4, полагавшиеся на кодовое имя, ломаются с явной ошибкой
  миграции: это заявленное breaking-изменение клиентских инструментов;
  серверный HTTP API не затронут.

## Вне scope v0.5 (осознанно)

Scoped IAM (OIDC/Keycloak, Groups, scoped Role Bindings), typed work items и
resource reservations v0.6, Positions/Assignments и reporting tree, Hire
Request и Engagement lifecycle, Worker Profiles, Dispatcher/Provisioner,
Execution Gateway, Operator UI, Kubernetes, микросервисы, Redis/Celery/Kafka,
универсальный plugin system, отдельная иерархия проектов, постоянный
dual-write с Git.

## Серия коммитов v0.5

```
v0.5 ADRs 0029-0041: Project Model contracts and reliability decisions
v0.5 P1: project model schema, per-tenant delivery isolation, X-Run-Id
v0.5 P2: project-aware tasks/discovery/context, operator surface, tests
v0.5 P3: close the compatibility window, project-aware clients, OpenCode adapter
v0.5 P4: benchmarks, full-system E2E, live Memory contract coverage
v0.5 docs and version 0.5.0
v0.5 adversarial review: fix confirmed findings across retention, governance and schemas
Assert the observation trace id in the live Memory contract test
Add the v0.5 baseline reference with measured results
Document the v0.5 settings in .env.example
Fix the two findings adversarial verification surfaced last
```

## Принятые ADR в service repository

ADR-0029 … ADR-0041 (реестр — [docs/adr/README.md](../adr/README.md)). Эти
решения считаются фактическими ограничениями для всех следующих версий.
