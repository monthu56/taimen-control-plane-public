# SPEC: Durable Child Run Handle (HRS-7)

Статус: реализовано; ADR-0046 принят после успешного spike

Target: `control-plane`

Источник задачи: `TASK-000007`

Источник контекста: `docs/reference/hermes-agent.md` верхнеуровневого
репозитория, разделы «Bounded дочерний результат и capability handle» и
«HRS-7. Durable Child Run Handle». Переносится форма контракта, не Hermes
state, class, dependency или naming.

## 1. Проблема

Дочернее исполнение сегодня выражается только парой «Task + relation
`spawned_by`». Этого достаточно для графа, но недостаточно для orchestration:

- **launch не идемпотентен.** Ambiguous response (таймаут, обрыв, retry) может
  создать вторую дочернюю Task с той же семантикой. Существующий HTTP
  `Idempotency-Key` защищает только повтор одного и того же HTTP-запроса и не
  переживает смену request id после restart оркестратора.
- **нет locator, переживающего restart.** Process-local subagent registry
  теряется, и единственный способ «вспомнить» дочернее исполнение — перечитать
  граф Task, что провоцирует перенос transcript и hidden reasoning как «памяти о
  запуске».
- **нет permission narrowing.** Дочерний Run сегодня ограничен только правами
  собственного API-ключа. Родитель не может выдать *меньше*, чем имеет сам, и
  сервер нигде не хранит потолок, который можно перепроверить.
- **нет bounded immutable terminal result.** `runs.output` — свободный JSONB без
  границ, хеша и запрета на перезапись; он не является доказуемым результатом
  для родителя.
- **cancellation policy неявна.** ADR-0044 каскадирует `force_cancel` по
  активным `spawned_by` descendants, но поведение кооперативного
  `request_cancel` относительно детей нигде не определено.
- **нет retention, expiry и revocation** для ссылки на дочернее исполнение.

## 2. Граница ответственности

Control Plane остаётся авторитетным для Task, Claim, Run, permissions, budgets,
approvals, artifacts и audit. Он **не** исполняет дочерний agent loop, не
запускает процессы и не переносит контекст между родителем и ребёнком.

Handle — это **versioned opaque locator и proof владения ссылкой**, а не
credential и не носитель authority. Любое обращение по handle выполняет
server-side lookup, tenant check и permission check заново. Handle,
предъявленный без валидного Bearer-ключа с нужным permission, не даёт ничего.

## 3. Use cases

| # | Кто | Что делает |
|---|---|---|
| U1 | Orchestrator | Запускает дочернюю работу, получает handle и продолжает свой turn |
| U2 | Orchestrator после restart | Читает Run Context, видит активные child handles, продолжает без transcript |
| U3 | Orchestrator после ambiguous response | Повторяет launch с тем же `correlationId` и получает тот же handle |
| U4 | Security review | Доказывает, что дочерний Run не мог выйти за потолок родителя |
| U5 | Родитель | Получает bounded terminal result с hash и ссылками на artifacts |
| U6 | Operator | Отзывает handle и, при необходимости, кооперативно останавливает ребёнка |
| U7 | Аудитор | Видит launch/resolve/revoke как события без payload и без reasoning |

## 4. Модель

### 4.1 Что durable и кто авторитетен

| Данные | Носитель | Авторитет |
|---|---|---|
| Существование и жизненный цикл дочерней работы | `tasks`, `task_claims`, `runs` | как сегодня, без изменений |
| Связь ребёнок→родитель | `task_relations` (`spawned_by`) | как сегодня |
| Идемпотентность launch, потолок прав, policy, expiry/revocation | `run_child_handles` (новая) | сервер |
| Bounded terminal result и его hash | `run_child_results` (новая, immutable) | сервер |
| Текущий статус исполнения ребёнка | **не дублируется**: выводится на чтении из дочерних Task/Run | Task/Run |

Ключевое решение: handle **не хранит** execution status. Дублирование статуса
создало бы второй источник истины и дрейф. Статус вычисляется join'ом на
дочерние Task/Run в момент resolution. Собственные поля handle — только те, у
которых нет носителя: `revoked_at`, `expires_at`, grant, policy, correlation.

### 4.2 `run_child_handles`

- `id`, `tenant_id`;
- `parent_run_id`, `parent_task_id`;
- `child_task_id`, `relation_id`;
- `child_run_id` — заполняется при первом `:start-run` дочерней Task под этим
  handle; после установки не меняется;
- `correlation_id` — bounded caller-chosen ключ, `^[A-Za-z0-9._:-]{1,128}$`;
- `secret_hash` — sha256 секретной части token'а (сам секрет не хранится);
- `handle_version` — версия формата token (`1`);
- `granted_permissions`, `granted_capabilities`, `granted_skills` — JSONB
  массивы: авторитетный потолок дочернего исполнения;
- `cancellation_policy` ∈ `cascade_cooperative | detach`;
- `depth` — расстояние до корневого Run; ограничено `max_child_depth`;
- `expires_at`, `revoked_at`, `revoked_by_principal_id`, `revoke_reason`;
- `created_by_principal_id`, `request_id`, `created_at`.

Инварианты БД:

- unique `(parent_run_id, correlation_id)` — источник идемпотентности launch;
- unique `(child_task_id)` — Task не может быть ребёнком двух handle;
- unique `(child_run_id)` where not null;
- composite FK, пиннящие все endpoints к одному `tenant_id`;
- check на `handle_version`, `cancellation_policy`, `depth >= 1`,
  `expires_at > created_at`;
- partial index по активным (не revoked, не expired) handle родителя.

### 4.3 `run_child_results`

Append-only, unique `(handle_id)`, UPDATE/DELETE запрещены триггером (как у
`run_harness_manifests`):

- `handle_id`, `tenant_id`, `child_run_id`;
- `outcome` ∈ `succeeded | failed | cancelled`;
- `summary` — bounded текст, 1..2000 символов;
- `data` — bounded JSONB под существующим `guard_json_document`;
- `artifact_refs` — ≤ 50 artifact id того же tenant и того же дочернего Run;
- `result_hash` = `"sha256:" + sha256(canonical({schemaVersion, outcome, summary,
  data, artifactRefs}))`, каноническое представление — то же, что у HRS-2
  (sorted keys, NFC, значимый порядок массивов, отказ от float);
- `recorded_at`.

Результат пишется **сервером** в той же транзакции, что и terminal transition
дочернего Run (`:succeed | :fail | :cancel`). Отдельного «публикующего» вызова
нет: иначе появился бы момент, когда Run завершён, а результата нет.

Если `runs.output` не влезает в границы — terminal transition отклоняется
`422 child_result_too_large` с указанием превышенной границы. Это осознанное
давление: объёмное содержимое обязано уходить в Artifact, а в результат —
ссылка. Terminal transition Run'а без handle этой проверке не подлежит.

### 4.4 Token

```
ch1_<base64url(handle_id)>_<base64url(secret_32_bytes)>
```

`ch1_` — версия формата. Проверка: разобрать префикс → декодировать id →
lookup строки → constant-time сравнение sha256 секрета → tenant → permission →
expiry/revocation. Секрет возвращается **один раз**, в ответе на создающий
launch; идемпотентный повтор возвращает `handleToken: null` (тот же приём, что
у создания API-ключа). Потеря token не блокирует работу: все операции доступны
по `handleId`, а сам список handle читается из Run Context и `GET
/runs/{id}/child-handles`. Token — удобный переносимый locator, не credential.

### 4.5 Permission narrowing

При launch сервер вычисляет
`granted = requested ∩ effective(parent)`, где `effective(parent)` — потолок
самого родительского Run: для корневого Run это permissions его principal, для
дочернего — `granted` его собственного handle. Отсюда монотонность: потолок
вниз по дереву только сужается.

Запрошенное сверх потолка не «поднимает» права и не является ошибкой
конфигурации по умолчанию: сервер отклоняет launch
`422 child_grant_exceeds_parent` со списком лишних элементов — тихое сужение
скрывало бы ошибку оркестратора.

Enforcement: любая authoritative операция, выполняемая **под дочерним Run**
(record action, create artifact, checkpoint, compile manifest, create control
message, terminal transition), проверяется против
`permissions(api_key) ∩ handle.granted_permissions`. Нарушение →
`403 child_grant_exceeded`. Capabilities/skills сужают видимость дочернего
`toolPolicy` в манифесте HRS-2: ребёнок не может увидеть skill, невидимый
родителю.

Handle не выдаёт и не наследует credentials: дочерний principal
аутентифицируется своим ключом. Handle только уменьшает то, что этот ключ может
сделать в границах дочернего Run.

### 4.6 Cancellation policy

| Событие у родителя | `cascade_cooperative` (default) | `detach` |
|---|---|---|
| `request_cancel` applied | сервер создаёт `request_cancel` control message активным дочерним Run | ничего |
| `force_cancel` | каскад по ADR-0044, **policy не спасает** | каскад по ADR-0044 |
| Родитель завершился сам | ничего; ребёнок продолжает | ничего |
| `:revoke` handle с `cancelChild=true` | `request_cancel` дочернему Run | то же |

Terminal state ребёнка **никогда** не распространяется вверх автоматически:
родитель узнаёт о нём через resolution и события. `detach` сознательно не даёт
иммунитета от `force_cancel` — это governance-остановка, а не кооперативный
сигнал.

## 5. Инварианты

| ID | Инвариант |
|---|---|
| I1 | Повтор launch с тем же `(parent_run_id, correlationId)` не создаёт второй дочерней Task/Run |
| I2 | Handle разрешается только внутри своего tenant; чужой id/token → `404` |
| I3 | `granted(child) ⊆ granted(parent)` транзитивно на любой глубине |
| I4 | Строка результата immutable: UPDATE/DELETE запрещены триггером |
| I5 | Результат существует тогда и только тогда, когда дочерний Run терминален |
| I6 | `result_hash` воспроизводим из сохранённого документа побайтово |
| I7 | Handle не хранит execution status; статус всегда выводится из Task/Run |
| I8 | Ни handle, ни результат, ни события не содержат transcript, raw prompt, hidden reasoning, secrets и абсолютных локальных путей |
| I9 | Revoked/expired handle не разрешается и не может быть использован для launch следующего уровня |
| I10 | Глубина дерева ограничена; цикл невозможен, так как ребёнок — всегда новая Task |
| I11 | Дочерний Run привязан ровно к одному handle, handle — ровно к одному дочернему Run |
| I12 | Launch, resolution и revoke не требуют и не принимают контекст родителя (никакого «наследования» prompt/history) |

## 6. API (`/api/v1`)

### `POST /runs/{parentRunId}/child-handles`

`tasks.claim` + holder живого Claim родительского Run. Требует
`Idempotency-Key`.

```json
{
  "correlationId": "review:migration-roundtrip",
  "title": "Проверить migration roundtrip",
  "description": "",
  "workspaceId": null,
  "projectId": null,
  "priority": "medium",
  "grant": {
    "permissions": ["tasks.read", "artifacts.write"],
    "capabilities": [],
    "skills": ["migration-check@2"]
  },
  "cancellationPolicy": "cascade_cooperative",
  "expiresInSeconds": 86400
}
```

`201` при создании, `200` при идемпотентном повторе:

```json
{
  "childHandle": {
    "id": "...",
    "parentRunId": "...",
    "childTaskId": "...",
    "childTaskPublicId": "TASK-000123",
    "childRunId": null,
    "correlationId": "review:migration-roundtrip",
    "grant": {"permissions": [], "capabilities": [], "skills": []},
    "cancellationPolicy": "cascade_cooperative",
    "depth": 1,
    "status": "pending",
    "expiresAt": "...",
    "revokedAt": null,
    "createdAt": "..."
  },
  "handleToken": "ch1_..."
}
```

`status` — производное поле чтения:
`pending | running | suspended | succeeded | failed | cancelled | revoked | expired`.

Дочерняя Task, relation `spawned_by` (child → parent) и строка handle
коммитятся одной транзакцией.

### `GET /runs/{parentRunId}/child-handles?cursor=&limit=&active=`

`tasks.read`. Стабильный порядок `(created_at, id)`, opaque cursor, default 50 /
max 200.

### `GET /child-handles/{idOrToken}`

`tasks.read`. Resolution: handle + производный статус + ссылка на результат.
Результат отдаётся встроенно (он bounded по построению).

### `POST /child-handles/{id}:revoke`

Holder живого Claim родительского Run **или** `claims.manage`. Требует
`Idempotency-Key`.

```json
{"reason": "no longer needed", "cancelChild": true}
```

Revoke идемпотентен: повторный вызов возвращает ту же строку. `cancelChild`
материализует `request_cancel` control message дочернего Run через существующий
контракт HRS-4, а не отдельным механизмом.

### Ошибки

- `403 forbidden | run_holder_mismatch | child_grant_exceeded`;
- `404 run_not_found | child_handle_not_found`;
- `409 run_not_active | stale_claim | child_handle_revoked | child_handle_expired`;
- `422 invalid_correlation_id | child_grant_exceeds_parent | child_depth_exceeded | child_result_too_large`;
- стандартный error envelope с `code`, `message`, `details`, `requestId`.

### Run Context

`GET /runs/{id}/context` дополняется `childHandles`: bounded список активных
handle (≤ 50 + `hasMore`) с id, correlationId, производным статусом и
`resultHash` терминальных. Это и есть механизм reconnect после restart.

## 7. События

- `run.child.launched` — ids, correlationId, depth, policy, grant sizes;
- `run.child.started` — при привязке `child_run_id`;
- `run.child.resolved` — outcome и `resultHash`;
- `run.child.revoked` — кто и `reason`;
- `run.child.cancel_requested` — при cascade от родителя.

Payload несёт ссылки и markers. `summary`, `data`, содержимое grant-намерений
сверх размеров и любой текст задачи в journal/outbox не попадают.

## 8. Failure model

| Сценарий | Поведение |
|---|---|
| Ambiguous response на launch | Повтор с тем же `correlationId` → `200` и тот же handle (I1) |
| Restart оркестратора | Handle читается из Run Context; token не нужен |
| Гонка двух launch с одним correlationId | Unique constraint сериализует; проигравший получает существующий handle |
| Потеря Claim родителя | Launch/revoke → `409 stale_claim`, без partial writes |
| `force_cancel` родителя во время launch | Lock order task → claim → run → handle даёт один serial order; поздний launch отклоняется как `run_not_active` |
| Дочерний Run падает без output | Результат пишется с `outcome=failed` и пустым `data`; hash считается от того же канонического документа |
| Handle истёк, ребёнок ещё работает | Resolution → `409 child_handle_expired`; ребёнок остаётся авторитетным и виден через Task/Run |
| Повторный terminal transition ребёнка | Уже существующая строка результата и unique `(handle_id)` не дают перезаписи |
| At-least-once доставка событий | Состояние таблиц авторитетно; события — уведомление |

## 9. Out of scope

- перенос transcript, raw prompts и hidden reasoning между Run;
- наследование credentials и выпуск дочерних API-ключей;
- process-local registry как источник истины;
- планировщик/очередь дочерних задач и автоматический подбор исполнителя;
- автоматическая запись Memory по результату ребёнка;
- streaming промежуточного прогресса ребёнка родителю;
- ADR — только после успешного spike (критерии перехода в референсе).

## 10. Совместимость

Изменение additive: новые таблицы, новые endpoint'ы, новое поле в Run Context.
Существующие `spawned_by` Task без handle остаются валидными и просто не имеют
handle-семантики; ADR-0044 cascade продолжает работать по relation, а не по
handle. Старые harness'ы не обязаны знать о child handles.

Новая harness capability: `child_run_handle.v1`.
