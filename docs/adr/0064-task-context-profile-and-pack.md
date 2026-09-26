# ADR-0064: Профиль контекста типа задачи, пакет контекста как evidence и `cp_recall`

Статус: Accepted (2026-09-25), TAI-ADR-0042 P2 (п.3–6)

Контекст: TAI-ADR-0042 (контекст задачи из базы знаний: доменные пакеты,
`context_schema` типа, пакет как evidence, `cp_recall`) и её амендмент «память
— только через Control Plane»; MEM-ADR-020 memory-service (реестр пакетов,
`POST /api/memory/context/typed`, амендмент «канал resolve»);
[ADR-0059](0059-task-context-pack-reaches-the-agent.md) (рендер пакета в
prompt, namespace workspace, сужение видимости);
[ADR-0060](0060-knowledge-snapshots-through-control-plane.md) (снимки и
пакеты через ядро); [ADR-0061](0061-approval-outcomes-declared-by-task-type.md)
(декларация в версии типа, грамматика `$.`-выражений);
[ADR-0062](0062-work-graph-goal-origin-acceptance.md) (evidence).

## Контекст

После P0/P1 граф знаний workspace заполнен сверкой снимков (пакет
`software-delivery`: эндпоинты, методы клиента, вызовы из UI, события,
таблицы, ADR, файлы), а исполнитель получает в prompt пакет свободного recall
(ADR-0059). Recall ищет по тексту: он не знает, что задаче про
`POST /tasks/{task_id}:claim` нужны **вызывающие** этого эндпоинта, и не
может обойти граф по типизированным связям на нужный момент. Кроме того,
пакет нигде не записан: ревьюер не может увидеть, что видел исполнитель.

## Решение

### 1. `task_types.context_schema` — часть иммутабельной версии типа

Как `approval_schema` (ADR-0061): колонка JSONB, `{}` — «профиля нет».
Триггер неизменяемости `task_types` покрывает колонку. Проверка — при
публикации версии (`domain/context_schema.py`), ошибка —
`422 invalid_context_schema` с `details.path`.

```yaml
contextSchema:
  anchors:
    - from: description                 # или title — текст
      kinds: [endpoint, adr, event, table]
    - from: "$.spawnedBy.artifact[commit].diffPaths"
      kind: source_file
      via: defined_in
    - from: "$.customFields.okpdCodes"  # значение поля — как есть
      kind: okpd_code
  traverse:
    - {relation: calls, direction: in, depth: 1, limit: 20}
    - {relation: governs, direction: in, from: previous}
  asOf: taskCreated                     # taskCreated | now | origin
  budgetTokens: 4000
```

- **`anchors[].from`**: `description` / `title` — текст, из которого
  извлекаются идентификаторы; иначе путь грамматики исходов ADR-0061 с
  корнями `$.task` (по умолчанию: `$.<поле>`, `$.customFields.<key>`) и
  `$.spawnedBy` (задача по связи `spawned_by`), включая
  `artifact[<тип>].<поле>` — сокращение `artifact[<тип>].metadata.<поле>`.
  `$.approval`, `!` и `|truncate` запрещены. Значение пути — строка или
  список строк — берётся якорем как есть.
- **`kind` / `kinds`** — имена видов пакета (`[a-z][a-z0-9_]{0,62}`) или их
  синонимы (`kindAliases` вида в пакете: `route` → `endpoint`). Для
  текста — чьи `idPatterns` применять (все виды каталога, если не указаны);
  кандидат несёт каноническое имя; порядок `kinds` — приоритет при отсечке
  по лимиту якорей. Для значения
  поля ровно один вид — подсказка Memory, несколько — без подсказки.
- **`via: <relation>`** — якорь заменяется сущностями, которые указывают на
  него этой связью (`endpoint -defined_in-> source_file`: изменённый файл
  приводит определённые в нём эндпоинты). Отдельный вызов обхода
  (`direction: in`, глубина 1) до основного.
- **`traverse`** — шаги обхода Memory: `relation`, `direction`
  (`in|out|both`, по умолчанию `out`), `depth` 1..5, `limit` 1..200 (20),
  `from` `anchors|previous`. Не больше 10 шагов, 20 якорей — лимиты
  `context/typed.py`: заведомо некомпилируемый профиль отвергается при
  публикации.
- **`asOf`**: `taskCreated` — создание задачи (по умолчанию); `now` —
  **момент взятия claim** (закреплён, чтобы пакет был воспроизводим);
  `origin` — `observedAt` наблюдения из `origin.evidence` (ADR-0057), иначе
  создание задачи.
- **`budgetTokens`** 1..32000 — доля prompt под типизированный пакет
  (рендер: 4 символа на токен, внутри общего бюджета ADR-0059).

Виды и связи ядру не известны и при публикации не сверяются с пакетами:
тип задачи общий для тенанта, а пакеты включаются по namespace workspace
(ADR-0060). Неизвестное имя даёт пустой обход, не ошибку.

### 2. Извлечение кандидатов якорей — в ядре, детерминированно

Шаблоны идентификаторов берутся **данными пакетов** из реестра Memory:
`GET /api/memory/namespaces/{ns}/kinds` (включённые пакеты
`name@version`) → `GET /api/memory/packages/{name}?version=` (`idPatterns`
видов). Версия пакета иммутабельна — скомпилированные шаблоны кэшируются в
процессе (до 64 версий); настройка namespace читается при каждой сборке.
Каталог, который не читается, — предупреждение: значения полей всё равно
становятся якорями.

`extract_identifiers`: виды в объявленном порядке, совпадения в порядке
текста; совпадение, целиком лежащее внутри более длинного совпадения
**любого** вида каталога, отбрасывается (`ADR-0058` внутри `CP-ADR-0058`,
путь внутри `POST <путь>`, `claims.py` внутри пути файла); пунктуация конца
предложения отрезается. Кандидат отправляется как написан: типизированный
обход Memory (memory-service eef1c70) разрешает якорь тем же
`resolve_candidates`, что канал `resolve`, — ключ, псевдоним, нормализация
параметров шаблона `{name} → {}`, суффикс пути — и сообщает способ
(`matchedBy`; несколько сущностей по неточной форме — `ambiguous`,
`candidates`). Ядро форм не размножает. Текст ограничен 20000
символов, кандидат — 300, якорей — 20. LLM и вектор не участвуют:
`allow_semantic: false`.

Шаблоны — данные tenant'а и могут откатываться экспоненциально, поэтому:
компилирует и исполняет их модуль `regex` (он отпускает GIL на время
сопоставления и принимает таймаут; stdlib `re` держит GIL, и поток не спасает
event loop), извлечение идёт в рабочем потоке (`anyio.to_thread`) в пределах
дедлайна запроса, на все шаблоны одного текста — 1 с, совпадений — не больше
1000; проверка вложенности совпадений — `O(n log n)`. Истёк таймаут —
текстовый источник не даёт якорей (частичный результат зависел бы от машины),
в `warnings` — строка об этом; значения полей остаются якорями.

Значение, похожее на секрет (те же шаблоны, что у комментариев и заметок
evidence, `domain/redaction.py`), якорем не становится — иначе оно ушло бы в
Memory и в запись пакета; в `warnings` — строка без самого значения.

Нормализация якорей повторяет канал `resolve` memory-service
(`context/resolve.py`); по ревью TASK-000281 типизированный обход
memory-service переходит на `resolve_candidates` отдельной задачей, после
неё копия в ядре удаляется (TODO в `domain/context_schema.py`).

### 3. Сборка и доставка (push)

`POST /context` с фокусом на задачу, тип которой объявляет профиль,
добавляет в ответ `taskContext`:

```json
{"status": "ok", "contextPackId": "…", "claimId": "…", "asOf": "…",
 "asOfMode": "taskCreated", "replayed": false, "recorded": true,
 "budgetTokens": 4000, "anchors": [{"kind", "value", "source", "via?"}],
 "warnings": [], "pack": {typed pack Memory}}
```

- Транзакционная половина читает источники якорей **правами вызывающего**
  (задача `spawnedBy` — при `tasks.read` на неё, артефакты — при
  `artifacts.read`), момент и claim; вызов Memory — после закрытия
  транзакции, параллельно с recall, в пределах того же дедлайна
  (`CP_CONTEXT_TIMEOUT_SECONDS + 1`).
- Namespaces и видимость — те же, что у recall этого запроса (ADR-0059 п.3):
  `tenant:<t>` + `tenant:<t>:ws:<корень>`, `allowedNamespaces`/`allowedScopes`
  из PDP в режиме policy, local-сужение до workspace фокуса, предков и
  principal.
- Статусы: `ok`, `empty` (ни одного якоря — Memory не вызывается),
  `unavailable`, `timeout`, `disabled` (провайдер не настроен или
  `includeMemory=false`), `forbidden` (нет `events.read`). Ошибка Memory
  деградирует только `taskContext`, `POST /context` отвечает 200.
- **Тип без профиля — прежнее поведение**: ключа `taskContext` в ответе нет,
  Memory не вызывается сверх recall.

`pack` в ответе урезан до `budgetTokens` профиля (без него — 3000, доля
рендера по умолчанию): сущности в порядке разделов, затем связи, пока их
оценка размера строки prompt (как в рендере, ≤ 600 символов на строку, 4
символа на токен) помещается; остаток — счётчики `omitted{entities, facts}`.
`used` и `unresolved` не урезаются: они описывают сборку, запись и
`:replay` сравнивают её целиком.

Рендер — общий `control_plane_agent.context_pack` (ADR-0059): разделы
типизированного пакета идут первыми внутри того же fence
`<recalled_memory>` — строка `task_context` (момент и id пакета), сущности
по видам (`ключ: заголовок (атрибуты) [anchor|inferred] [source: …]`),
раздел `relations` (`subject relation object`); те же правила очистки строк
и бюджет. Адаптеры Claude Code, Codex и OpenCode получают его без изменений.

### 4. Пакет — evidence работы задачи, по одному на claim

Пакет привязан к claim. Первое чтение контекста **держателем активного
claim** собирает пакет и записывает его. Claim перепроверяется при записи под
блокировкой строки задачи (claim, release и перехват берут ту же
блокировку, истечение — срок `expiresAt`): если за время сборки claim
отпущен, истёк или сменился, пакет не записывается (`recorded: false`,
предупреждение).

- таблица `task_context_packs` (append-only, триггер; уникально по
  `claim_id`): `request` — тело типизированного запроса как отправлено
  (якоря, обход, `as_of`) без полей видимости, `namespaces`, `as_of` /
  `as_of_mode`, `candidates` (откуда каждый якорь), `used` (`entities`
  `{namespace, natural_key}`, `facts` — id, `snapshots` —
  `{source, scope, snapshot_id}`), `unresolved`, `trace_id`, версия типа,
  кто собрал;
- связь пакета с задачей — сама запись (`task_id`, `claim_id`); документ
  задачи **не пишется**: ни `evidence`, ни `version`. Иначе чтение контекста
  между чтением задачи и `PATCH` с её версией (`cp_get_task` →
  `cp_get_context` → `cp_update_task`) давало бы `409`, а каждый повторный
  claim дописывал бы `evidence` до предела в 200 элементов. Сослаться на
  пакет в `evidence` можно явно — вид `{"kind": "context_pack",
  "contextPackId"}` ADR-0062 (существование проверяется в tenant'е, `404`
  для чужого/неизвестного); найти пакеты задачи — по событиям ниже или
  `taskContext.contextPackId`;
- событие `task.context_pack_recorded` (`contextPackId`, `claimId`, момент,
  счётчики сущностей/фактов/снимков) по потоку задачи — в память не
  переносится (нет в whitelist mapping).

Повторное чтение в том же claim (перезапуск исполнителя, `cp_get_context`)
**повторяет записанный запрос** (`replayed: true`) — исполнитель видит тот же
пакет весь claim. Новый claim собирает пакет заново («пересборка при claim»).
Сборка идёт при первом чтении контекста, а не в `:claim`: claim — путь
координации, и недоступность памяти не должна его замедлять (ADR-0025).
Чтение задачи не-держателем (ревьюер, оператор) собирает пакет без записи
(`recorded: false`); если в текущем claim пакет уже записан, не-держатель
получает его повтор с той же редакцией якорей, что `GET /context-packs/{id}`
(ниже): якоря недоступных ему источников, их значения в запросе к Memory и
`redactedAnchors` — как там; вырезаны все якоря — `status: empty`, Memory не
вызывается.

**Воспроизводимость.** `GET /context-packs/{id}` — запись; `POST
/context-packs/{id}:replay` отправляет записанный запрос снова (namespaces
записи, видимость — текущего вызывающего) и сравнивает `used`:
`reproduced`, `drift{missingEntities, extraEntities, missingFacts,
extraFacts}`, `pack`. Момент закреплён в запросе, а Memory хранит закрытые
факты с интервалом валидности, поэтому закрытие связи после момента пакета
его не меняет; `drift` означает, что история под моментом изменилась (или у
читателя другая видимость).

Права на `GET` и `:replay`: `tasks.read` на задачу пакета и `events.read` —
то, что пакет использовал, это durable memory, и читатель без `events.read`
получает в `/context` `memoryStatus: forbidden`. Пакет собран правами
держателя claim, поэтому якоря из источников, которые читатель сам не
прочёл бы, вырезаются (`redactedAnchors` — сколько): метаданные артефактов —
без `artifacts.read`, задача `spawnedBy` — без `tasks.read` на неё. Вместе с
якорем уходят его значения из `request.anchors`, `unresolved` и
`used.entities` (совпадение ключа со значением). `:replay` отправляет
запрос после той же редакции — `drift` у такого читателя ожидаем.

### 5. `cp_recall` (pull)

`POST /api/v1/context/recall` и MCP `cp_recall(anchor|query, relations,
depth, as_of, direction, kind, kinds, limit, task)`:

- ровно одно из `anchor` (идентификатор как написан) и `query`
  (идентификаторы извлекаются так же, как из описания задачи; если их нет —
  один якорь текстом запроса с `allow_semantic: true`, Memory помечает такие
  находки `evidence: inferred`), иначе `422 invalid_recall_request`;
- `relations` — шаги обхода от якорей (`direction` по умолчанию `both`,
  `depth` 1..5, `limit` 1..200); `asOf` — ISO 8601 с поясом, без него —
  «сейчас»;
- namespaces — из `task` (MCP: текущая задача) или `workspaceId`, никогда от
  клиента; видимость — как у `/context`; право `events.read`
  (`403`), задача — `tasks.read`;
- `budgetTokens` 1..32000 (3000) — `pack` урезан так же, как `taskContext`;
- `503 memory_disabled` (провайдер не настроен) / `memory_timeout`,
  `502 memory_unavailable` (ошибка Memory, как ADR-0060).

MCP `cp_recall` возвращает `text` — рендер тем же `render_graph_pack`, что и
раздел prompt, в пределах `budget_tokens` — и якоря, момент, namespaces,
предупреждения; сырой пакет — только с `include_pack=true`, чтобы ответ
инструмента не съедал контекст агента. Один клиент памяти
(`HttpContextProvider`) и один формат для push и pull.

### 6. Контракт Memory

`POST /api/memory/context/typed` (`ContextIn`: тело запроса +
`scope{namespace, namespaces}` + `allowedNamespaces`/`allowedScopes`),
`GET /api/memory/namespaces/{ns}/kinds`, `GET /api/memory/packages/{name}`
— закреплены `tests/fixtures/memory_graph_contract.json`: схемы из
`app.openapi()` memory-service (коммит `7f43a8c`), правила тела
`TypedContextRequest.from_payload`, ответы реестра для пакета
`software-delivery` суперпроекта, сгенерированные кодом memory-service.
Тела `HttpContextProvider` проверяются против них
(`tests/unit/test_graph_memory_contract.py`), тестовая память
(`tests/fake_graph_memory.py`) валидирует каждый запрос тем же контрактом.

### 7. Миграция `b7e4d2a9c6f1` (после `f4c1e8b2a9d7`, M1.3 ADR-0063)

`task_types.context_schema` (JSONB, default `{}`) + пересоздание триггера
неизменяемости; таблица `task_context_packs` с триггером append-only.
Таблиц других ветвей не касается. Downgrade теряет данные: профили и записи
пакетов удаляются, явно процитированные элементы evidence `context_pack`
остаются висячими ссылками.

## Профиль `coding-task` (пакет `software-delivery`)

Новая версия `coding-task` в `packages/core/task-types/coding-task.yaml`
суперпроекта (правится там, не в этом репозитории) — в словаре фактического
пакета (`integrations/selfdev/.../software-delivery.yaml`: связи `calls`,
`defined_in`, `emits`, `governs`, `part_of`; связей `decided_by`, `writes` и
вида `config_flag` из примера TAI-ADR-0042 в пакете нет):

```yaml
contextSchema:
  anchors:
    - from: description
      kinds: [endpoint, adr, event, table]
  traverse:
    - {relation: calls, direction: in, depth: 1, limit: 20}   # кто вызывает
    - {relation: defined_in, direction: out, depth: 1}        # где определено
    - {relation: governs, direction: in, from: previous}      # какой ADR управляет файлом
  asOf: taskCreated
  budgetTokens: 4000
```

Якорь `$.spawnedBy.artifact[commit].diffPaths` с `via: defined_in` из
TAI-ADR-0042 грамматикой поддержан, но в профиль не включён: артефакт
`commit` раннера пока не несёт списка изменённых путей, а ключ `source_file`
в пакете — `<repo>:<path>`.

## Последствия

- Задача с профилем получает в prompt контракты, связанные с упомянутыми в
  ней сущностями, а ревьюер — запись того, что видел исполнитель, и способ
  её воспроизвести.
- Новая зависимость рантайма — `regex` (шаблоны пакетов с таймаутом без
  блокировки event loop), для mypy — `types-regex`.
- Пакет стоит ещё 1–3 вызова Memory на первое чтение контекста в claim
  (каталог namespace, пакет — один раз на процесс, `via`, обход) и один на
  повторное; recall идёт параллельно.
- Видимость пакета — видимость держателя claim; `:replay` другим
  читателем может показать `drift` из-за другой видимости, а не истории.
- Событие `context.drifted` (TAI-ADR-0042 п.5, P3) не реализовано; сравнение
  `:replay` — ручной вариант той же проверки.
- Разрешение якоря (нормализация `{name}`, суффиксы, неоднозначность) —
  целиком на стороне Memory; ядро от него зависит и своей копии не держит.

## Не принято

- **Собирать пакет в `POST /tasks/{id}:claim`.** Claim — путь координации;
  вызов памяти в нём связал бы доступность claim с доступностью Memory.
- **Хранить пакет копией.** Evidence — указатели (ADR-0062): запись хранит
  запрос и id использованного, содержимое восстанавливается запросом на
  закреплённый момент.
- **Передавать описание задачи в Memory целиком и извлекать идентификаторы
  там.** Извлечённые ядром кандидаты записываются в пакет (что и откуда
  стало якорем), их порядок и лимит объяснимы; шаблоны при этом остаются
  данными пакета.
- **Проверять виды и связи профиля по пакетам при публикации.** Тип задачи
  — уровень tenant'а, пакеты включаются по workspace; одна версия типа
  работает в workspace с разными пакетами.
