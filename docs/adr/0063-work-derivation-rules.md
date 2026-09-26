# ADR-0063: Правила вывода работы — данные тенанта, движок в worker'е

Статус: Accepted (2026-09-24), M1.3; реализует в control-plane п.3–4 и 6
TAI-ADR-0036 «Вывод Work из состояния — наблюдения, правила и reconciliation»
(суперпроект); амендмент 2026-09-25 (M1.6, TASK-000392) — `complete_work`,
`acceptance` у `ensure_work`, evidence решения в задаче, отложенное решение
под claim (реализация — TASK-000395); амендмент 2026-09-25 (oss-sync,
TASK-000456) — `fields.customFields` у заводящих действий; амендмент
2026-09-25 (TASK-000444) — `request_decision` заводит gate-approval

Контекст: TAI-ADR-0036; ADR-0057 (внешние наблюдения — вход правил);
ADR-0062 (origin `rule` + `ruleId` + evidence — выход правил); ADR-0056
(вызов Skill ядром через `skill_invocation`); ADR-0061 (исходы approval —
тот же путь `invokeSkill`, та же модель полномочий); ADR-0023/0036
(курсор журнала и изоляция по tenant'ам); TAI-ADR-0044 (каталог пакетов).

## Контекст

Работу в ядре заводили человек, агент по ходу работы или исход approval.
Наблюдения уже приходят (git-коннектор пишет `repo.commit_observed` на
staging), у задачи уже есть `origin.kind = rule` с `ruleId` и evidence
(ADR-0062), но того, кто сопоставляет факты с желаемым состоянием и заводит
работу, не было. Нужен механизм, который:

- продуктово-нейтрален: что наблюдается, какой скилл интерпретирует факт и
  какой тип задачи заводится, — данные тенанта, а не код ядра;
- детерминирован: условие — ограниченный язык, без кода и без LLM; всё, что
  требует интерпретации, делает Skill с объявленным контрактом, и его
  результат — факт (evidence), а не «мнение правила»;
- не порождает дублей и не теряет фактов при повторной доставке журнала;
- объясним: по каждой задаче видно, какое правило, какой версии, по какому
  факту и с каким результатом её завело.

## Решение

### 1. Правило — данные: `work_rules`

`POST /api/v1/rules` принимает документ:

```json
{
  "key": "adr-conformance",
  "description": "…",
  "workspaceId": null, "goalId": null,
  "trigger": {"kind": "observation", "type": "repo.commit_observed"},
  "condition": {"exists": "payload.data.repo"},
  "interpretation": {"skill": "adr.conformance_check@1",
                     "inputs": {"repository": "{{payload.data.repo}}",
                                "ref": "{{payload.data.sha}}"}},
  "action": {"kind": "ensure_work", "taskType": "coding-task",
             "forEach": "skill.output.results",
             "where": {"ne": [{"var": "item.status"}, "implemented"]},
             "dedupKeyTemplate": "adr-conformance:{{payload.data.repo}}:{{item.adr}}",
             "fields": {"title": "{{item.adr}}: {{item.status}}",
                        "description": "Пробелы: {{item.gaps}}"}},
  "status": "enabled"
}
```

- `key` — идентичность правила в tenant'е (`^[a-z0-9][a-z0-9._-]{0,127}$`),
  уникален среди неархивных правил (частичный уникальный индекс); занятый
  ключ — `409 rule_key_taken`.
- Правило изменяемо (как WorkspaceType в TAI-ADR-0044, а не как
  иммутабельная версия типа задачи): `PATCH` с `If-Match: "rule-<v>"`
  меняет `description`, `trigger`, `condition`, `interpretation`, `action`,
  `goalId`; `version` растёт с каждым изменением того, что правило делает.
  Каждая оценка записывает версию, по которой считалась. `key` и
  `workspaceId` после создания не меняются. PATCH, повторяющий текущие
  значения, — не изменение.
- Статус `enabled | disabled | archived`. `:enable` / `:disable` —
  идемпотентны. `DELETE` архивирует: правило больше не оценивается и не
  меняется, история и заведённая им работа остаются, ключ освобождается.
- `goalId` — цель, которой служит заведённая работа (`tasks.goal_id`);
  проверяется как привязка задачи к цели (ADR-0062 п.2).
- При записи проверяется всё, что можно проверить без фактов: грамматика
  документов (п.2), существование типа задачи по ключу
  (`422 unknown_task_type`) и скилла по `name@version` (`422 unknown_skill`),
  отказ от скилла с `sideEffects: external_write`
  (`422 rule_skill_side_effects`: у правила нет approval, который мог бы быть
  основанием внешней записи по ADR-0056 §4), секреты во всех документах
  правила, включая константы условия (`422 secret_material_rejected`). Ни одного слова о предметной области в
  схеме и коде нет.

### 2. Язык: JSON-выражения и шаблоны, без кода

**Выражение** — `true`/`false` или объект с одним оператором:
`and`/`or` (список 1..50), `not`, `eq`/`ne`/`lt`/`le`/`gt`/`ge` (два
операнда), `in` (элемент ∈ список), `exists` (путь не `null`). Операнд —
`{"var": "<путь>"}`, `{"const": <JSON>}`, скаляр или список операндов.
Путь — `корень(.сегмент)*`, сегмент `[A-Za-z0-9_-]{1,64}` (индекс списка —
число). Больше ничего нет: ни вызовов, ни арифметики, ни регулярных
выражений. Глубина ≤ 16, узлов ≤ 256, документ ≤ 16 KiB. Невалидное условие —
`422 invalid_rule_condition` при записи, а не при срабатывании.

Корни фиксированы по стадии: условие и входы скилла читают `trigger`
(вид, тип, ссылка на событие, время), `payload` (тело события журнала — для
наблюдения это `kind`, `content`, `data`, `source`, `externalRef`…), `goal`
(цель правила: `id`, `title`, `status`, `workspaceId`) и `task` (задача, на
которую ссылается событие: сущность `task` или `payload.taskId`); действие
вдобавок читает `skill` (`status`, `output`, `invocationId`, `artifactId`)
после интерпретации и `item` внутри `forEach`. Корень, недоступный на
стадии, — отказ при записи.

Семантика: отсутствующее значение — `null`; равенство — строгое
JSON-равенство (`true ≠ 1`); сравнение порядка — только число с числом или
строка со строкой, с `null` — ложь; иное сочетание (строка с числом) —
**ошибка оценки** (`failed`, `rule_condition_error`), а не молчаливая ложь:
сломанное правило должно быть видно.

**Шаблон** — строка с `{{ путь }}`. Строка, целиком состоящая из одного
плейсхолдера, даёт сырое значение (список остаётся списком — так во вход
скилла передаются массивы), иначе значения подставляются текстом.

### 3. Триггеры и чтение журнала

- `observation` — `{"kind": "observation", "type": "<kind наблюдения>",
  "source"?}`: событие `observation.recorded` с этим `kind` (и `source`).
- `event` — `{"kind": "event", "type": "<тип события журнала>"}`. Типы
  `rule.*`, `work.*` и `skill.invocation_*` запрещены
  (`422 invalid_rule_trigger`), `observation.recorded` — через вид
  `observation`. Жизнь вызова скилла пишет его исполнитель, а не правило,
  которое вызов поставило: правило на `skill.invocation_succeeded` с
  интерпретацией ставило бы на каждый успех новый вызов, успех которого снова
  его запускал бы (два таких правила — пинг-понг).
- `schedule` — `{"kind": "schedule", "type": "interval", "everySeconds":
  60..604800}`: первая оценка — на ближайшем проходе после включения, затем
  раз в интервал; пропущенные за время простоя слоты не догоняются (одна
  оценка). У расписания нет своего факта, поэтому правило по расписанию,
  которое заводит работу, обязано иметь `interpretation` (`422 invalid_rule`):
  результат скилла — единственное, на что такая работа может сослаться.

Журнал читает worker своим курсором `work-rules` в
`event_consumer_cursors` (по строке на tenant, тот же порядок
`(tx_id, sequence)` и стабильный горизонт, что у Context Adapter, архив
журнала включительно). Курсор tenant'а создаётся вместе с его первым
правилом — в «настоящем» (после последнего события ниже горизонта), история
журнала не переигрывается. Пакет событий оценивается и курсор сдвигается
**в одной транзакции** под блокировкой строки курсора
(`FOR UPDATE SKIP LOCKED`): несколько реплик worker'а не оценивают один
tenant одновременно, падение повторяет пакет, а уникальность
`(rule_id, trigger_ref)` в `rule_evaluations` делает повтор пустым.
Неожиданная ошибка откатывает пакет и откладывает tenant с backoff
(`failure_count`, `next_attempt_at`, `parked_reason` видны в строке курсора);
курсор не перепрыгивает через пакет — факты не теряются.

Каждая оценка пакета идёт в своём savepoint'е. Пока пакет упал меньше
`CP_RULES_MAX_ATTEMPTS` (3) раз подряд, ошибка откатывает его целиком (сбой
мог быть преходящим). Дальше оценка, которая всё ещё ломается, фиксируется
как `failed` с кодом `rule_internal_error` (текст ошибки — в `error`, событие
`rule.evaluated`), и пакет идёт дальше: одно сломанное правило не
останавливает все правила tenant'а. Недоступность внешнего решения
(`503 decision_unavailable` — PDP в режиме `policy`, ADR-0055) — не вердикт
по правилу: она никогда не записывается как `failed`, а всегда откатывает
пакет (backoff курсора), ожидающую оценку откладывает, слот расписания
закрывает как сбой (ниже). Факт, на котором PDP был недоступен, будет
оценён, когда PDP вернётся.

Сбой прохода по расписанию закрывает этот слот оценкой `failed`
(`rule_internal_error`) и сдвигает `next_run_at` на следующий слот, а не
повторяется на каждом тике worker'а: у расписания нет своего факта, который
можно потерять, а его интервал (≥ 60 с) и есть backoff.

Правило видит только события, записанные после его включения
(`occurred_at ≥ enabled_at`): включение не оценивает задним числом то, что
пришло, пока правило было выключено. Правила не реагируют на следствия
правил: пропускаются события с корреляцией `work-rule:<id>`, события
сущности `rule` и события сущности `skill_invocation` тех вызовов, что
поставило правило (ключ идемпотентности `work-rule:<evaluationId>`, п.4:
завершение вызова пишет исполнитель своей корреляцией). Вместе с запретом
триггеров `skill.invocation_*` это гарантирует, что правило не может
кормить само себя — даже правило, записанное до запрета.

**Воркспейс.** Журнал читается по tenant'у. Событие, которое называет
воркспейс (`payload.workspaceId`, как у наблюдения с воркспейсом), доходит
только до правил этого воркспейса и правил уровня tenant'а. Событие без
воркспейса — факт всего tenant'а и доходит до всех правил, в том числе
воркспейсных: его данные могут попасть в задачу воркспейса правила. Это
сознательно: у большинства событий ядра воркспейса в payload нет, и
отсекать их значило бы лишить воркспейсные правила событий ядра. Кто пишет
правило воркспейса, отвечает за то, какие поля факта он переносит в работу;
чтение фактов требует `events.read` на воркспейсе правила (п.6).

### 4. Интерпретация — вызов Skill тем же путём, что `invokeSkill`

Если у правила есть `interpretation`, после истинного условия ядро ставит
вызов через `invoke_skill` — тот же путь, что `POST /skills/{ref}:invoke` и
`invokeSkill` исходов approval (ADR-0061): контракт, схема входов, права
вызывающего, `requiredPermissions`, ключ идемпотентности
`work-rule:<evaluationId>`. Задачи у вызова нет (работы ещё нет), поэтому
основание внешней записи невозможно — такие скиллы отсечены при записи
правила. Ссылка на скилл — только `name@version`: интерпретация не меняется
под правилом при публикации новой версии.

Оценка переходит в `waiting` и **не держит worker**: отдельный проход
возвращается к ней раз в `CP_RULES_SKILL_CHECK_SECONDS` (15 с). Когда вызов
закончился:

- `succeeded` — выход скилла записывается артефактом `skill_result` (без
  задачи, в воркспейсе правила, автор — полномочия правила; в `metadata` —
  `ruleId`, `ruleVersion`, `evaluationId`, `invocationId`), артефакт
  добавляется в evidence оценки, и выполняется действие;
- `failed` / `cancelled` — оценка `failed` (`rule_skill_failed`, причина
  вызова в `details`), работа не заводится: без интерпретации правило не
  гадает;
- вызов, который никто не взял за `CP_RULES_SKILL_WAIT_SECONDS` (сутки),
  отменяется системой — дальше как `cancelled`;
- правило за время ожидания изменилось (другая `version`) или выключено —
  оценка `skipped` (`rule_changed` / `rule_stopped`), живой вызов
  отменяется: ответ применило бы правило, которого уже нет.

### 5. Действия и идемпотентность

Словарь закрыт: `ensure_work`, `update_work`, `cancel_work`,
`request_decision`. `forEach` (путь к списку, ≤ 50 элементов) и `where`
(условие над `item`) применяют действие к каждому отобранному элементу —
так один результат скилла по многим ADR даёт по работе на каждое
несоответствие. Ни одного отобранного элемента — `not_matched`.

`dedupKeyTemplate` даёт ключ работы (≤ 200 символов). Ключ **общий для
tenant'а** (как ключ `ensureWork` исхода approval): журнал
`rule_work_items` связывает ключ с задачей и с правилом, которое её завело;
поэтому второе правило (`cancel_work`, `update_work`) сверяет работу,
заведённую первым. Операции по одному ключу сериализуются advisory-lock'ом
на транзакцию. Оценка сначала вычисляет ключи всех своих элементов
(`forEach`) и берёт lock'и в отсортированном порядке — две оценки над
одними ключами (пакет журнала, расписание, возобновлённое ожидание) не
встают в deadlock. Пакет держит lock'и всех своих оценок до коммита, поэтому
цикл между разными оценками пакета и параллельной оценкой не исключён; его
разрывает детектор deadlock'ов PostgreSQL, и проигравшая сторона
повторяется (пакет — с backoff курсора, ожидание — позже), факт не теряется. Задачу, которую `update_work` / `cancel_work` будут менять,
оценка блокирует (`FOR UPDATE`) до записи: параллельная правка человеком
ждёт правило, а не превращает его ожидаемую версию в `version_conflict`.
Найденная задача, которую полномочия правила не могут читать, — `404`, как
несуществующая.

- `ensure_work` — если по ключу есть открытая задача, это она (`created:
  false`); иначе `create_task` с `taskType`, полями `title`, `description`,
  `priority`, `assignee`, воркспейсом и целью правила и
  `origin = {kind: rule, ruleId: <id правила>, ref: rule_evaluation:<id>,
  evidence: [наблюдение, артефакт скилла]}` (ADR-0062: `rule` обязан
  назвать правило и хотя бы один факт). Закрытая работа по ключу не
  блокирует новую: расхождение, вернувшееся после закрытия, — новая работа.
  Evidence триггера: наблюдение — `{kind: observation, observationId}`,
  событие ядра — `{kind: external, externalRef: {system: control-plane,
  id: event:<id>}}`.
- `request_decision` — то же, плюс **gate**-approval (ADR-0018) на созданную
  задачу (`fields.approver` — принципал или `fields.approverRole` — роль):
  работа ждёт решения, решение исполняет исходы `approvalSchema` типа задачи
  (ADR-0061, амендмент TASK-000444 ниже); при найденной открытой задаче
  второй запрос не создаётся.
- `update_work` — открытой задаче по ключу меняет `title` /
  `description` / `priority` и дописывает в её evidence факты оценки,
  которых там нет; нет открытой задачи — `skipped: no_open_work`.
- `cancel_work` — переводит открытую задачу в первый статус категории
  `terminal_cancelled`, достижимый по lifecycle её типа; такого нет —
  `failed: rule_cannot_cancel`.
- Работа под живым claim'ом не переписывается правилом: `update_work` /
  `cancel_work` дают `skipped: task_claimed`.

Отказ любой команды откатывает всё, что действие записало (savepoint), и
оценка становится `failed` с кодом команды; факты оценки (evidence,
артефакт) остаются.

### 6. Полномочия правила

Правило действует **полномочиями того, кто его включил** (или последним
изменил включённое): снимок credential'а (`authority`, форма
`decision_authority` исходов approval) и `authority_principal_id`. Каждая
оценка проверяет, что credential ещё активен (общий с исходами
`require_active_credential`: ключ не отозван и не истёк, IAM-binding
активен, принципал активен), и задаёт каждому действию обычные проверки
команд тем же authorizer'ом, что и API (`CP_AUTHZ_MODE`). Чтение фактов
требует `events.read` на воркспейсе правила, чтение цели — `goals.read`,
задачи триггера — `tasks.read`. Правило не может сделать больше, чем его
автор руками; отзыв ключа автора останавливает правило (`failed:
credential_inactive`), а не даёт ему работать от имени ядра.

### 7. Аудит

- Жизнь правила: `rule.created`, `rule.updated` (`changes` — имена полей,
  `version`), `rule.enabled`, `rule.disabled`, `rule.archived` — сводка без
  шаблонов: ключ, версия, статус, триггер, скилл, вид действия, тип задачи.
- Каждая завершённая оценка — одно `rule.evaluated` (сущность `rule`):
  `ruleId`, `ruleKey`, `ruleVersion`, `evaluationId`, `triggerRef`,
  `result` (`matched | not_matched | failed | skipped`),
  `conditionMatched`, `evidence` (указатели), `skillInvocationId`, `work`
  (ключ, задача, создана/пропущена), `error.code`.
- `work.derived` (сущность `task`) — работа заведена правилом;
  `work.reconciled` — изменена или отменена (`update_work` / `cancel_work`);
  оба несут правило, версию, оценку, ключ и evidence.
- История — `GET /rules/{id}/evaluations` (новые первыми, фильтр
  `status`): `rule_evaluations` хранит версию правила, `trigger_ref`
  (`event:<id>` или `schedule:<epoch>`), результат, evidence, вызов скилла,
  `created_task_ids` (оценка с `forEach` может завести несколько задач,
  поэтому список, а не одно поле `created_task_id` из постановки) и ошибку.

Context mapping памяти (ADR-0062 п.7) правила не переносит: `rule.*` и
`work.*` в whitelist не добавлены — в память по-прежнему попадают
`task.created` с `origin` (там `ruleId` и evidence).

### 8. Права `rules.read` / `rules.write`

Отдельно от `tasks.*` и `goals.*`: правило заводит работу само, долго после
того, как его написали, и право заводить работу руками не даёт права это
автоматизировать. Решаются на воркспейсе правила (правило уровня tenant'а —
на tenant'е); в `authz/catalog.yaml` — `resource: workspace`, список в режиме
`policy` сужается до воркспейсов с `rules.read` плюс правила уровня tenant'а.

**Развёртывание: права существующих binding'ов.** `rules.read` /
`rules.write` — новые действия; их нет ни у одного существующего ключа и
binding'а, кроме `admin`. Выдача — шаг `deploy/bootstrap.py` суперпроекта
(правит владелец, не эта ветка), в том же релизе, что миграция
`f4c1e8b2a9d7`:

- оператору (человеку-владельцу) — `rules.read` и `rules.write`; у него же
  должны быть `events.read`, `tasks.write` (и `skills.invoke`, если правило
  интерпретирует факты, `approvals.manage` для `request_decision`,
  `goals.read` для правил с целью) — полномочия, которыми правило будет
  действовать;
- агентам-исполнителям и харнессу — `rules.read` (объяснить задачу с
  `origin.kind = rule` через `cp_get_rule`); `rules.write` — не выдаётся:
  правило, заведённое агентом, действовало бы полномочиями агента без
  решения человека;
- в режиме `policy` — те же действия в binding'ах policy-service на тех же
  scope, что `goals.*`, после регистрации обновлённого `authz/catalog.yaml`.

До этого шага `/rules` отвечает `403` всем, кроме `admin`.

### 9. API, SDK, MCP

`POST/GET /rules`, `GET/PATCH/DELETE /rules/{id}`, `POST /rules/{id}:enable`,
`:disable`, `GET /rules/{id}/evaluations`; фильтры списка `status`,
`workspaceId`, `key`, `triggerKind`. SDK: `create_rule`, `list_rules`,
`get_rule`, `update_rule`, `enable_rule`, `disable_rule`, `archive_rule`,
`list_rule_evaluations`. MCP: `cp_list_rules` и `cp_get_rule` (read-only;
`cp_get_rule` возвращает и первую страницу истории оценок) — чтобы агент мог
объяснить, откуда задача. Мутирующих MCP-инструментов для правил нет:
правило — решение владельца, пишется через API или пакет.

Настройки worker'а: `CP_RULES_BATCH_SIZE` (200 событий на пакет tenant'а),
`CP_RULES_SKILL_CHECK_SECONDS` (15), `CP_RULES_SKILL_WAIT_SECONDS` (86400),
`CP_RULES_MAX_ATTEMPTS` (3 неудачных пакета до изоляции сломанной оценки, п.3).

### 10. Миграция `f4c1e8b2a9d7` (после `d2f7a3c9b1e5`)

Таблицы `work_rules` (CHECK: статус; триггер и действие — объекты с
`kind`; включённое правило имеет `enabled_at` и полномочия), составной FK
`(tenant_id, goal_id) → goals`; `rule_evaluations` (уникальность
`(rule_id, trigger_ref)`, `next_check_at` задан ровно у `waiting`);
`rule_work_items`. Данных нет. Downgrade лишает данных: правила, история и
журнал ключей удаляются вместе со строками курсора `work-rules`; работа,
заведённая правилами, остаётся с `origin.kind = rule`.

### 11. `WorkRule` в каталоге пакетов (TAI-ADR-0044)

Правила — данные, и их место — пакет рядом со скиллами и типами задач,
которые они называют. Вид — `WorkRule`, в обёртке TAI-ADR-0044 без слоя
перевода:

```yaml
apiVersion: <группа>/v1          # как у остальных видов пакета (TAI-ADR-0044)
kind: WorkRule
key: adr-conformance          # → поле key правила
spec:                         # ровно тело POST /rules без key, camelCase
  description: Accepted ADR выполняются в коде на каждом наблюдённом коммите
  trigger: {kind: observation, type: repo.commit_observed}
  condition: {in: [{var: payload.data.repo}, [control-plane, memory-service]]}
  interpretation:
    skill: adr.conformance_check@1
    inputs: {repository: "{{payload.data.repo}}", ref: "{{payload.data.sha}}"}
  action:
    kind: ensure_work
    taskType: coding-task
    forEach: skill.output.results
    where: {ne: [{var: item.status}, implemented]}
    dedupKeyTemplate: "adr-conformance:{{payload.data.repo}}:{{item.adr}}"
    fields:
      title: "{{item.adr}}: {{item.status}} в {{payload.data.repo}}"
      description: "Пробелы: {{item.gaps}}"
```

Семантика применения — как у изменяемых видов (WorkspaceType, Role):
найти по `key` (`GET /rules?key=`), создать при отсутствии, при
расхождении `spec` — `PATCH` с `If-Match`; удаления нет (`retire` файла
установки — `DELETE`, то есть архив). Ссылки — по ключам
(`taskType`, `skill: name@version`), `workspaceId`/`goalId` — топология
инсталляции и в пакет не входят (правило пакета — уровня tenant'а).
`${NAME}` — как для остальных видов.

Сам вид добавляется в суперпроекте (`packages/schema/v1`, установщик
`tools/cp_packages.py`, пилотное правило — в `packages/selfdev/rules/`), а
не этой веткой: исполнитель control-plane правит только control-plane.
Со стороны ядра пакету всё готово: документ правила — ровно тело API, его
проверку `check` может вызвать как `normalize_rule_spec` из
`domain/work_rules.py`. Пилот заводится этим файлом. Условие пилота
отбирает репозитории, из которых коннектор шлёт наблюдения с проверяемыми
ADR. Наблюдение не несёт списка затронутых ADR (коннектор пишет `repo`,
`sha`, `pack`, счётчики), поэтому скилл сверяет все Accepted ADR
репозитория на этом коммите, а дедуп по `repo:adr` не даёт повторным
коммитам плодить дубли.

## Границы

- Нет периодического reconciliation целей (TAI-ADR-0036 п.5) как отдельного
  контроллера: «желаемое множество работы» выражается правилами, а
  `update_work` / `cancel_work` отдельного правила сверяют уже заведённую
  работу по ключу.
- Условие не читает факты памяти (memory-service): только журнал, Work
  Graph (задача, цель) и результат скилла. Факт памяти можно получить
  скиллом.
- Правило не вызывает скиллы с внешней записью и не действует своим именем:
  у него нет ни approval-основания, ни собственных прав.
- Отложенный (parked) курсор `work-rules` не выведен в операторские действия
  (`/operations`, ADR-0037): он сам повторяет пакет с backoff; ручной
  redrive — отдельное решение, если понадобится.

## Последствия

- Работа может появляться без человека и без агента — из фактов, по данным
  тенанта; каждая такая задача несёт правило, оценку и факты, а история
  правила показывает, на что оно смотрело и что сделало.
- Первое правило пилота (саморазработка) — файл пакета, а не код ядра.
- Worker получил третий вид работы (после outbox и исходов approval):
  чтение журнала со своим курсором и возврат к оценкам, ждущим скилл.
- Вид `WorkRule` в каталоге пакетов и выдача `rules.*` существующим
  binding'ам — шаги суперпроекта (п.8, п.11).

## Амендмент 2026-09-25: закрытие работы правилом, `acceptance` у `ensure_work`, решение под claim

Основание: фича `verification-reconciliation` (M1.6, spec/plan в суперпроекте,
дизайн одобрен владельцем в TASK-000379); стадия проверки —
[ADR-0067](0067-verification-stage.md). Решение записано в T001
(TASK-000392), реализуется в T004 (TASK-000395). Существующие правила
продолжают работать как раньше, кроме одного: `update_work` / `cancel_work`
на работе под живым claim больше не пропускаются (А4).

### Проблема

(а) Выполненная работа, чья предпосылка исчезла потому, что её сделали,
закрывалась `cancel_work` — **отменённой**: в метриках успех выглядел как
отказ. (б) Правило не могло задать критерии приёмки заводимой работы, и
стадии проверки (ADR-0067) было нечего исполнять у работы правил. (в) Решение
правила об отмене жило только в событии `work.reconciled`: по самой задаче не
видно, на каком факте её закрыли. (г) Работа под живым claim'ом давала
`skipped: task_claimed` — исполнитель продолжал тратить ресурс на ненужное,
а решение правила терялось.

### А1. Действие `complete_work`

Словарь п.5 дополняется: `ensure_work`, `update_work`, `cancel_work`,
**`complete_work`**, `request_decision`. `complete_work` — открытой задаче по
ключу:

- дописывает в её `evidence` факты оценки (наблюдение-триггер, артефакт
  скилла) с `check: <key>` — ключ критерия задаётся полем действия `check`
  (шаблон); без него — ключ неявного критерия (ниже);
- запускает завершение задачи **через стадию проверки** (ADR-0067, попытка с
  `trigger = rule`, `trigger_ref = rule_evaluation:<id>`), а не выставляет
  статус: обходного пути к «выполнено» нет;
- если у задачи нет acceptance — попытка идёт с одним **неявным** критерием
  `external_state` (ключ `rule-evidence`), который проходится только что
  записанным evidence; итог — `task.completed` и `task.verified`;
- нет открытой задачи по ключу — `skipped: no_open_work`; закрытая —
  ничего не меняет (повторное наблюдение не даёт дублей, FR-011).

`work.reconciled` несёт `action: complete_work`. Отвергнуто: `update_work` со
статусом (обходит стадию) и закрытие отменой (SC-003).

### А2. `ensure_work.acceptance`

Необязательное поле действия `acceptance` у `ensure_work` и
`request_decision` — список критериев в форме ADR-0062 п.4 с грамматикой
`spec` ADR-0067 п.4. Строки могут быть шаблонами правила (`{{…}}`, п.2) с
корнями стадии действия. **При публикации** правила документ проверяется
`normalize_checks` (`422 invalid_rule_action` с кодом и путём ошибки
грамматики в `details`), кроме значений, целиком состоящих из шаблона, — у
них проверяются пути шаблона; **при исполнении** отрендеренный список ещё раз
проходит `normalize_checks` в `create_task`, как у любой задачи. Найденная по
ключу открытая задача acceptance не получает («ensure», не upsert).

Отвергнуто: acceptance по умолчанию у типа задачи (новое поле версии типа
ради одного пилота), наследование критериев от цели (контроллера целей нет).

### А3. Evidence решения — в задаче

`cancel_work` и `complete_work` записывают факты оценки в `evidence`
**самой задачи** (не только в `work.reconciled`), без дублей: элемент,
который уже есть, второй раз не пишется. У `cancel_work` — без `check`; у
`complete_work` — с `check` (А1). Задача, закрытая правилом, отвечает «на
каком факте» без чтения журнала (FR-009).

### А4. Решение под живым claim

Пункт п.5 «работа под живым claim'ом не переписывается правилом» меняется:

- `update_work` — по-прежнему `skipped: task_claimed` (поля работы в руках
  исполнителя);
- `cancel_work` / `complete_work` при живом claim с активным run вызывают
  существующий `request_cancel_run` (полномочия правила: `tasks.write` и
  `claims.manage`) **в том же проходе** и переводят оценку в `waiting` с
  `next_check_at` — тот же механизм возобновления, что у ожидания скилла
  (п.4). Решение записано как ожидающее;
- на возобновлении: claim снят (run завершён, claim освобождён или истёк) —
  решение применяется **ровно один раз**; задача уже в `terminal_success` —
  `skipped: already_done` (результат исполнителя не перезаписывается);
  задача уже в `terminal_cancelled` — `skipped: already_closed`; claim не
  освободился за `CP_RULES_CLAIM_WAIT_SECONDS` — `failed: claim_not_released`;
- live claim без run (харнесс человека) — запрос отмены некому отправить:
  оценка так же ждёт освобождения claim'а.

Отвергнуто: немедленная отмена задачи под claim (ломает fencing
исполнителя), прежний пропуск (исполнитель тратит ресурс, решение теряется).

### А5. `task` в выражениях правила

Представление задачи в корне `task` (п.2) дополняется `verification` —
сводкой последней попытки проверки `{status, attempt}` или `null`, чтобы
условие правила могло отличить проверяемую работу от брошенной.

### А6. Не входит

Закрывающие правила пакета саморазработки (`ci-green`,
`adr-conformance-resolved`, перевод `submodule-lag-resolved` на
`complete_work`) и вид `WorkRule` с новыми полями в схеме пакетов — шаг
суперпроекта (T006). Пакет-фикстура второго домена — T005.

### Реализация амендмента (T004, TASK-000395)

- **Словарь.** `ActionKind.COMPLETE_WORK`; `complete_work`, как и
  `cancel_work`, не берёт `fields`. Поле `check` — только у `complete_work`:
  литерал проверяется по форме ключа критерия при записи, шаблон — по путям,
  отрендеренный ключ — при исполнении (`invalid_rule_field`). Правило по
  расписанию с `complete_work` без интерпретации отвергается
  (`invalid_rule`), как заводящее: неявному критерию нечего процитировать.
- **`acceptance` при записи** — `normalize_checks` над списком, где значения,
  целиком состоящие из шаблона (весь критерий, `key`, `kind`,
  `description`), заменены заглушками; грамматика `spec` (`check_spec`) —
  у критериев с литеральным `kind` и `spec` без шаблонов. Ошибка — `422
  invalid_rule_action`, `details.cause` — код грамматики, `details.field` —
  путь (`action.acceptance[i].spec…`).
- **Неявный критерий** — `{key: rule-evidence, kind: external_state}`
  (`RULE_EVIDENCE_CHECK` в `domain/work_graph.py`). Evidence с `check:
  rule-evidence` принимается у любой задачи (ключ принадлежит правилу, а не
  объявленному критерию). Завершение идёт через `complete_task` с
  `trigger = rule`, `triggerRef = rule_evaluation:<id>` и `implicit_checks`:
  попытка исполняет их, только если у задачи нет своих критериев. Worker
  проходит правила раньше проверок, поэтому пара правил доводит работу до
  `done` за один цикл.
- **Работа уже сдана** (открыта попытка) — `complete_work` только дописывает
  evidence, второй попытки нет (FR-011). Запись evidence в задачу (правилом
  или `PATCH`) будит попытку в `waiting_external` (`wake_on_evidence`).
- **Evidence решения** (А3) пишется через `update_task` — те же права и
  событие `task.updated`; тождество элемента — вид, id факта и `check`.
- **Ожидание claim** (А4). Живой claim — тот же критерий, что у записи
  задачи (`live_claim_of`: claim активен, не истёк, сессия держателя жива).
  Оценка переходит в `waiting` с `result.waitingFor = claim`,
  `waitingSince` и элементом `work[]` `{waiting: true, reason: task_claimed,
  claimId, runId?, cancelRequested?}`; `rule.evaluated` пишется один раз — при
  окончательном исходе. Возобновление — `CP_RULES_SKILL_CHECK_SECONDS`, срок —
  `CP_RULES_CLAIM_WAIT_SECONDS` (по умолчанию сутки). Отказ
  `request_cancel_run` полномочиям правила не проваливает оценку: код — в
  `cancelError`, решение ждёт, пока исполнитель закончит сам. Run, начатый
  под новым claim'ом за время ожидания, тоже получает запрос отмены.
- **На возобновлении** кроме `already_done` / `already_closed` /
  `claim_not_released`: `cancel_work` на задаче, сданной на проверку за время
  ожидания, — `skipped: verification_pending` (результат исполнителя
  проверяется и отменой не выбрасывается). Правило, изменённое или
  выключенное за время ожидания, — `skipped: rule_changed | rule_stopped`, как
  у ожидания скилла: решение применило бы правило, которого уже нет. Строка
  оценки берётся `FOR UPDATE SKIP LOCKED` и выходит из `waiting` в той же
  транзакции, что применяет решение, — ровно один раз.
- **`task.verification`** (А5) — `{status, attempt}` последней попытки.
- **Исполнитель слушается запроса отмены.** Демон раннера
  (`control_plane_agent/supervision.py`, общий для адаптеров Claude Code,
  Codex и OpenCode) во время исполнения читает run не реже раза в 30 с
  (`CONTROL_PLANE_AGENT_CONTROL_POLL_SECONDS`, 15): `cancelRequestedAt` —
  исполнитель останавливается (отмена asyncio-задачи; CLI убивает процесс,
  OpenCode получает abort), принятые control messages подтверждаются
  (`request_cancel` — `applied`, остальные — `superseded`), run — `:cancel`
  с причиной `cancel_requested`, claim освобождается. Тем же механизмом —
  **сторож «нет прогресса»**: run без новых действий
  `CONTROL_PLANE_AGENT_STALL_WARN_SECONDS` (600) — checkpoint `stall` с
  последним действием; `CONTROL_PLANE_AGENT_STALL_STOP_SECONDS` (1800) —
  остановка, run `failed` с причиной `no_progress`, claim освобождён.
  Незавершённое последнее действие (`status: started`) считается живым:
  checkpoint `stall` пишется по тому же порогу с `actionRunning: true` и
  пометкой «action still running: <action> since <startedAt>», а остановка —
  по `CONTROL_PLANE_AGENT_ACTION_MAX_SECONDS` (3600, не меньше порога
  остановки); завершение этого действия — прогресс. Сторож смотрит только
  на поток действий run. SDK — `list_run_actions`.

## Амендмент 2026-09-25 (oss-sync): `fields.customFields` у заводящих действий

Основание: фича `oss-sync` (spec/plan в суперпроекте, дизайн одобрен
владельцем 2026-09-25, TASK-000449), FR-007 и FR-010; реализация — O002
(TASK-000456). Работа, которую заводит правило, получала только `title`,
`description`, `priority`, `assignee`: всё, что исполнителю нужно знать о
предмете работы в машинном виде (ревизия, ветка, компонент), приходилось
вписывать в текст описания и разбирать обратно. Исход approval
(`ensureWork.customFields`, амендмент ADR-0061) это уже умеет; правило — нет.
Существующие правила продолжают работать без изменений.

### Б1. `fields.customFields`

У `ensure_work` и `request_decision` в `fields` — необязательный объект
`customFields`: имя поля → строка-шаблон (п.2) с корнями стадии действия
(`forEach` даёт `item`, интерпретация — `skill`).

- **При записи правила** (`POST` / `PATCH /rules`) проверяется только
  форма: непустой объект, не больше 32 полей, имена
  `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, значения — строки, пути шаблонов — из
  допустимых корней. Нарушение — `422 invalid_rule_action`,
  `details.field` — путь (`action.fields.customFields.<имя>`). Соответствие
  `fieldSchema` при записи не проверяется: работа закрепляет версию типа,
  активную в момент заведения, а не в момент записи правила.
- **При заведении работы** значения рендерятся как любое поле: строка,
  целиком состоящая из одного шаблона, сохраняет сырое значение факта
  (число остаётся числом), иначе — подстановка текста. Значение, которое
  отрендерилось в `null` или пустую строку, **опускается** (как в
  `ensureWork` исхода approval): обязательное поле схемы тогда сообщается
  отсутствующим, а не заводится пустым. Итог проходит `fieldSchema` типа в
  `create_task`, как у любой задачи; несоответствие — оценка `failed` с
  кодом `custom_fields_invalid`, savepoint откатывает всё, что действие
  записало, работы нет, факты оценки остаются.
- **Найденная по ключу открытая работа** полей не получает («ensure», не
  upsert — как `acceptance`, А2).
- **`update_work`, `cancel_work`, `complete_work`** `customFields` не берут
  (`422 invalid_rule_action`): поля заведённой работы принадлежат её
  исполнителю, а правило, переписывающее их, спорило бы с ним.

Отвергнуто: проверка значений `fieldSchema` при записи правила (значения —
шаблоны, их форма известна только на фактах; версия типа к моменту
срабатывания может смениться); `update_work.customFields` (поля в руках
исполнителя, п.5 и А4 — тот же довод); заполнение отсутствующих значений
пустой строкой (скрывало бы сломанный путь шаблона).

### Б2. Нейтральность

Ядро не знает, какие поля бывают: имена и смысл — данные типа задачи и
правила пакета. Проверка — пакет-фикстура второго домена `invoice-payment`
(не из саморазработки): его правило `invoice-received` пишет в работу
реквизиты счёта, а тип отвергает сумму не своей формы.

## Амендмент 2026-09-25 (TASK-000444): `request_decision` заводит gate

**Проблема.** `request_decision` заводил обычный (совещательный) approval,
тогда как остальные, кто заводит решение по задаче, — `ensureWork.requestApproval`
исхода (ADR-0061) и критерий `human` (ADR-0067) — заводят gate. Исходы
`approvalSchema` типа задачи `decide_approval` планирует только для gate
(ADR-0061 п.4), поэтому решение по задаче из правила их не исполняло: на
staging одобренный approval задачи типа `invoice-payment` остался с
`outcomeStatus = null`, `invokeSkill notify.send@1` не был вызван.

**Решение.** `request_decision` заводит approval с `gate = true`. Следствия —
общие для любого gate, ничего нового в ядре:

- решение (`approve` / `reject`) задачи, тип которой объявляет исходы для
  этого решения, ставит `outcome_status = pending`, и worker исполняет
  `approved` или `rejected` с полномочиями решившего; тип без исходов —
  `outcome_status` остаётся `NULL`;
- **до решения задачу нельзя взять и завершить**: `:claim` и `:complete`
  отвечают `409 approval_required` (ADR-0018). Это держит работу у **любого**
  типа, тип gate не снимает: правило `request_decision` с типом без исходов
  раньше давало задачу, которую можно было взять до решения, теперь — нет.
  Так и задумано («работа ждёт решения»), но это смена поведения для
  опубликованных правил; нужен ли режим «решение не держит работу» —
  вопрос владельцу, в этом амендменте его нет;
- `complete_work` того же правила по задаче с открытым gate отказывает
  `approval_required`, как любой `:complete` под gate; `cancel_work` проходит;
- критерию `human` задачи (ADR-0067 §6) такой gate засчитывается по тем же
  правилам, что любой gate: если его исход `completeTask` сдал задачу или
  если он одобрен после открытия попытки.

Отвергнуто: gate только при объявленных типом исходах (поведение approval'а
зависело бы от версии типа, а не от правила; «ждёт ли работа решения» —
решение автора правила, а не типа).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "work_rules"'}
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "rule_evaluations"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'event_type="work\.(derived|reconciled)"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'event_type="rule\.evaluated"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'invoke_skill\('}
- grep: {path: src/control_plane/domain/work_rules.py, pattern: '"skill\.invocation_"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'sorted\(dedup_keys\)'}
- grep: {path: src/control_plane_mcp/server.py, pattern: 'async def cp_get_rule'}
- absent: {path: "src/control_plane/domain/work_rules.py", pattern: '(?i)conformance|\bgit\b|repositor'}
- absent: {path: "src/control_plane/application/commands/*rule*.py", pattern: '(?i)conformance|\bgit\b|repositor'}
- grep: {path: src/control_plane/domain/work_rules.py, pattern: 'COMPLETE_WORK = "complete_work"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'request_cancel_run\('}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: '"already_done"'}
- grep: {path: tests/integration/test_rules_closing_m16.py, pattern: 'test_a_pair_of_rules_closes_the_work_as_done_on_two_observations'}
- grep: {path: tests/integration/test_rules_closing_m16.py, pattern: 'test_a_closing_rule_on_claimed_work_stops_the_run_and_applies_once'}
- grep: {path: src/control_plane_agent/supervision.py, pattern: 'NO_PROGRESS = "no_progress"'}
- grep: {path: tests/client/test_agent.py, pattern: 'test_a_cancel_request_stops_the_adapter_and_cancels_the_run_once'}
- grep: {path: src/control_plane/domain/work_rules.py, pattern: 'CUSTOM_FIELDS = "customFields"'}
- grep: {path: src/control_plane/application/commands/rule_evaluations.py, pattern: 'custom_fields=_custom_fields\(fields\)'}
- grep: {path: tests/integration/test_rules_custom_fields.py, pattern: 'test_values_outside_the_field_schema_fail_the_evaluation_and_file_nothing'}
- grep: {path: tests/integration/test_invoice_payment_package.py, pattern: 'test_an_invoice_the_type_does_not_accept_files_no_work'}
```
