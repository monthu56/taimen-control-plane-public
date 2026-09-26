# TASK-000005: Scoped Tool Discovery View — SPEC

Статус: completed; ветка `feat/hrs-3-scoped-tool-discovery`

Target: `control-plane`

Источник задачи: `TASK-000005`

Референс: внешний исследовательский артефакт Hermes Agent HRS-3

## 1. Проблема и граница

Большой MCP/plugin catalog нельзя целиком помещать в prompt. Progressive
disclosure решает бюджет контекста, но создаёт новый риск: bridge вида
`tool_search` / `tool_describe` / `tool_call` легко превращается в обход
permissions, если поиск отвечает по всему registry, а вызов доверяет тому, что
schema однажды попала в prompt.

В v0.7 Control Plane уже хранит registry инструментов (`skills`), назначения
(`principal_skills`), governance проекта (`allowedSkillProtocols`) и
self-declared harness capabilities (`skills.protocol.*` сессии). Но:

- нет bounded projection, пригодной для поиска: `GET /harness/context`
  отдаёт **все** назначенные skills целиком и не масштабируется;
- нет revision/hash, по которым клиент мог бы инвалидировать кэш;
- `record_run_action` резолвит **любой** skill тенанта: назначение и
  governance при исполнении повторно не проверяются.

Граница: Control Plane не исполняет tools. Он ведёт catalog, вычисляет
effective policy, отдаёт bounded projection и **повторно авторизует** действие
в момент, когда Run заявляет его как выполненное. Prompt assembly, кэш на
стороне harness и сам вызов остаются вне сервера.

## 2. Три слоя и их различие

| Слой | Вопрос | Источник |
|---|---|---|
| Capability Catalog | что runtime технически умеет | `skills` тенанта |
| Effective Tool Policy | что разрешено этому Principal/Run/workspace сейчас | catalog ∧ assignment ∧ status ∧ governance ∧ harness capability |
| Tool Discovery View | bounded проекция разрешённого пересечения | search/describe |
| Action Authorization | можно ли это исполнить прямо сейчас | пересчитывается при invocation |

Инвариант, ради которого слои разделены: **discovery никогда не является
источником права**. Между `describe` и `call` мир мог измениться, поэтому
authorization пересчитывается заново и не смотрит на то, что клиент уже видел
schema.

## 3. Пользователи и use cases

- Harness с большим catalog ищет инструмент по задаче, а не грузит всё.
- Harness подгружает полную schema ровно того инструмента, который собирается
  вызвать.
- Harness с eager-режимом (малый catalog) получает тот же ответ первой
  страницей и не нуждается в отдельном API.
- Operator объясняет, почему инструмент был виден или не виден в конкретном
  Run: `provenance.toolPolicy` манифеста ссылается на те же revisions.
- Harness кэширует projection и инвалидирует её по `viewHash`.
- Принципал, которому инструмент не назначен, не может ни найти его, ни
  описать, ни исполнить.

## 4. Effective Tool Policy: решение и причины

Решение по каждому инструменту принимает **одна** чистая функция
(`domain/tool_discovery.py`), которую используют все три потребителя: HRS-2
манифест, discovery view и invocation authorization. Это не оптимизация, а
единственный способ гарантировать, что manifest, поиск и вызов не разойдутся.

Два измерения намеренно не схлопываются в один boolean:

- **authorization** — назначение принципалу, статус skill, governance проекта;
- **capability** — умеет ли harness исполнять этот protocol.

`visible = authorized ∧ capable`.

Причины (`reason`), стабильные строки:

| Reason | Значение |
|---|---|
| `assigned_and_protocol_supported` | виден |
| `not_assigned` | инструмент не назначен принципалу |
| `skill_disabled` | версия отключена в registry |
| `protocol_not_allowed_by_governance` | `allowedSkillProtocols` проекта |
| `protocol_not_supported_by_harness` | сессия не объявила `skills.protocol.<p>` |

`not_assigned` и `skill_disabled` никогда не попадают наружу как отдельный
ответ discovery: инструмент просто отсутствует в projection (см. §7).

### Почему harness capability не участвует в invocation

Объявленные capabilities — это **запись утверждения клиента, а не полномочие**
(тот же принцип, что в HRS-2 для declared sections). Строить на них запрет
исполнения означало бы, что клиент управляет собственной авторизацией. Поэтому
invocation re-authorization проверяет только authorization-измерение, а
несовпадение capability фиксируется в metadata действия как
`capabilityMismatch`. Инструмент, не разрешённый authorization-измерением,
отклоняется независимо от того, что объявил клиент.

## 5. Revisions и инвалидация кэша

Ревизии **выводятся**, а не хранятся отдельной таблицей: любое изменение
registry или policy обязано менять их, а отдельный счётчик — это ещё одно
место, где можно забыть его увеличить.

- `catalogRevision = sha256(canonical([[skillId, rowVersion, status], ...]))`
  по всем skills тенанта. Меняется при create/update/delete/disable.
- `policyRevision = sha256(canonical({assignments, governance, projectConfig,
  harnessCapabilities}))` — всё, что сужает catalog до конкретного Run.
- `viewHash = sha256(canonical({catalogRevision, policyRevision, query,
  limit, cursor}))` — идентичность конкретной страницы projection.

Канонизация и хэш живут в `domain/canonical.py` (выделены из HRS-2 и
переиспользованы здесь): один алгоритм и один формат `sha256:<hex>` на всю
платформу.

`viewHash` отдаётся как `ETag`; повтор с `If-None-Match` даёт `304`. Смена
любой ревизии меняет `viewHash`, поэтому устаревшая projection не может быть
подтверждена как свежая.

## 6. Bounded projection

Search-элемент (summary) содержит: `id`, `name`, `version`, `protocol`,
`status`, `summary` (усечённое до 200 символов описание), `visible`, `reason`.

Describe-элемент дополнительно: полное `description` (до 4 000 символов) и
`inputSchema`.

Никогда не проецируются: `skills.config` (endpoints, headers, connection
material), `outputSchema` (не нужен для решения о вызове), любые поля вне
whitelist.

`inputSchema` проходит **whitelist-санитайзер**: сохраняются только
структурные ключевые слова JSON Schema (`type`, `properties`, `required`,
`items`, `enum`, `title`, `description`, `format`, `minimum`, `maximum`,
`minLength`, `maxLength`, `pattern`, `oneOf`, `anyOf`, `allOf`, `$ref`,
`additionalProperties`, `nullable`). Всё остальное — `default`, `examples`,
`x-*` vendor extensions, `$comment` — удаляется: именно туда в реальных MCP
манифестах попадают endpoints, токены и внутренние адреса. Удалённые ключи
перечисляются в `schemaRedactions`, чтобы санитизация была видимой, а не
молчаливой.

После санитизации документ проходит существующий детектор secret material;
находка означает **fail closed**: `inputSchema` заменяется на
`{"redacted": true}` с причиной, инструмент остаётся видимым (иначе секрет в
описании инструмента прятал бы сам инструмент от операторов).

Границы: `limit` default 25, max 100; `query` максимум 200 символов;
санитизация ограничена глубиной `MAX_JSON_DEPTH` и 32 КБ на schema.

## 7. HTTP API (`/api/v1`)

### `GET /tools?query=&runId=&limit=&cursor=`

Требует `tasks.read` (тот же уровень, что и остальной harness-контекст).
`runId` необязателен: без него policy считается для принципала и его активной
сессии, с ним — для конкретного Run (governance проекта берётся из его task).

```json
{
  "items": [
    {
      "id": "…", "name": "repo.search", "version": "1.2.0",
      "protocol": "mcp", "status": "active",
      "summary": "Search files in the target repository",
      "visible": true, "reason": "assigned_and_protocol_supported"
    }
  ],
  "nextCursor": null,
  "view": {
    "catalogRevision": "sha256:…",
    "policyRevision": "sha256:…",
    "viewHash": "sha256:…",
    "mode": "search",
    "returned": 1,
    "hasMore": false
  }
}
```

Ответ содержит **только** инструменты, прошедшие authorization-измерение.
Инструменты, отсечённые capability-измерением, включаются с `visible: false` и
причиной: harness должен понимать, что инструмент ему назначен, но он сам не
объявил поддержку протокола — это диагностика конфигурации, а не утечка.

`query` пустой → eager-режим (`"mode": "eager"`), первая страница того же
множества. Один dataset и один код для обоих режимов; это то, что делает
сравнение режимов в §11 честным.

### `GET /tools/{toolRef}`

`toolRef` — UUID или `name@version` / `name`. Отдаёт describe-проекцию.

Инструмент вне authorization-измерения даёт `404 tool_not_found` —
**тот же ответ**, что и несуществующий инструмент. Отличать «нет такого» от
«есть, но не для тебя» означает отдать enumeration oracle по именам.

### Ошибки

- `404 tool_not_found` — нет в catalog **или** вне effective policy;
- `403 forbidden` — нет `tasks.read`;
- `404 run_not_found` — `runId` чужой или несуществующий;
- `422 invalid_tool_query` — превышены границы query/limit;
- `422 invalid_cursor`;
- стандартный error envelope с `code`, `message`, `details`, `requestId`.

## 8. Invocation re-authorization

`record_run_action(skill_ref=…)` перестаёт резолвить произвольный skill
тенанта. Новый порядок:

1. резолв ссылки (UUID / `name@version` / `name`) — как раньше;
2. пересчёт effective policy для (principal, run, project governance)
   в той же транзакции, под теми же locks, что и запись действия;
3. `authorized == false` → `403 tool_not_authorized` с `reason`, действие не
   записывается, budget не расходуется;
4. `authorized == true` → действие записывается; при
   `capable == false` в `metadata.capabilityMismatch` фиксируется protocol.

Discovery-результат в решении не участвует: даже если инструмент был в
projection минуту назад, отзыв назначения между `describe` и `call` даёт
отказ.

Отказ **не** порождает доменное событие: отклонённый вызов откатывает
транзакцию, и событие либо исчезло бы вместе с ней, либо потребовало бы
отдельного соединения ради записи не-факта. След отказа — счётчик
`tool_invocation_denied_total` в `/metrics` (низкой кардинальности, без имён
инструментов) и server-side лог. Аргументы вызова и payload не пишутся никуда,
а имя инструмента, которым управляет вероятный злоумышленник, не попадает в
durable journal.

## 9. Provenance в Effective Harness Manifest

`base.toolPolicy` получает `catalogRevision` и `policyRevision`;
`provenance.toolPolicy` — `revisions` с теми же значениями. Manifest
становится проверяемым ответом на вопрос «почему этот инструмент был виден в
этом Run»: ревизии из манифеста подставляются в discovery и дают ту же
projection.

Это меняет `baseHash` для новых манифестов (существующие строки immutable и не
переписываются). Совместимость: `schemaVersion` манифеста остаётся 1, секция
дополняется полями — читатели, обращающиеся по имени поля, не ломаются.

## 10. Failure model

- **Гонка discovery/invocation**: между поиском и вызовом отозвано назначение →
  `403 tool_not_authorized`. Проверка идёт под теми же locks, что и запись.
- **Изменение catalog во время пагинации**: cursor привязан к
  `(name, id)`, а `view.catalogRevision` в каждом ответе позволяет клиенту
  заметить смену; страницы разных ревизий не смешиваются молча.
- **Секрет в schema**: fail closed — `inputSchema` редактируется.
- **Огромный catalog**: bounded `limit`, обязательная пагинация, отсутствие
  «отдай всё» режима.
- **Enumeration probing**: единый `404` и отсутствие различий в тайминге
  между «нет» и «не разрешено» (оба пути идут через один resolve).
- **Tenant isolation**: все запросы фильтруются по `tenant_id`; чужой UUID
  даёт `404`, а не `403`.
- **Legacy-сессия без `skills.protocol.*`**: считается capability-agnostic,
  видит всё, что разрешено authorization-измерением (поведение
  `resolve_executable_skills` сохранено).

## 11. Измерения (acceptance criterion 3)

Reference dataset: синтетический catalog из 500 инструментов и 20 задач с
известным правильным инструментом. Скрипт `scripts/tool_discovery_benchmark.py`
сравнивает режимы на одном dataset:

| Метрика | Eager | Search |
|---|---|---|
| input tokens (оценка по canonical bytes / 4) | все schemas | summary страницы + 1 describe |
| model round trips | 1 | 2 |
| task success (найден правильный инструмент) | — | — |
| false-negative discovery (правильный есть в policy, но не найден) | — | — |

Числа фиксируются в `docs/verification/TASK-000005-verification.md`.

## 12. Out of scope

- универсальный marketplace и UI каталога;
- импорт Hermes tool registry, любые Hermes-specific имена и зависимости;
- автоматическая выдача permissions по результатам поиска;
- семантический/векторный поиск (лексический матч + границы; вектор — после
  измеримой потребности);
- исполнение инструментов Control Plane'ом;
- хранение аргументов и результатов вызовов.

## 13. Acceptance и verification

1. Инструмент вне policy не находится, не описывается и не исполняется.
2. Смена `catalogRevision`/`policyRevision` меняет `viewHash` и инвалидирует
   старую projection (`ETag`/`If-None-Match`).
3. Измерены task success, input tokens, round trips, false-negative discovery.
4. Search и describe не раскрывают `config`, `default`, vendor extensions и
   secret material.
5. Invocation re-authorization и tenant isolation закреплены contract- и
   security-тестами.
6. Миграция upgrade/downgrade/upgrade и полный test/lint/type gate.
