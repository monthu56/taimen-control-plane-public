# ADR-0067: Стадия проверки — критерии приёмки исполняются до «выполнено»

Статус: Accepted (2026-09-25), M1.6, фича `verification-reconciliation`
(spec/plan в суперпроекте, дизайн одобрен владельцем в TASK-000379). Реализуется
по шагам: T001 (TASK-000392) — это решение и грамматика `acceptance[].spec`;
T002 (TASK-000393) — попытки, критерий `deterministic`, провал (см.
«Реализация T002»); T003
(TASK-000394) — `human` и `llm_judge` через решение approval (см.
«Реализация T003»); T004
(TASK-000395) — правила ([ADR-0063](0063-work-derivation-rules.md), амендмент
2026-09-25, и «Реализация T004» ниже);
T005 (TASK-000396) — пакет-фикстура второго домена (см. «Реализация
T005»).

Контекст: ADR-0062 (acceptance и evidence задачи — п.4 оставлял `spec` без
интерпретации до M1.6); ADR-0063 (правила вывода работы); ADR-0061 (исходы
approval: `invokeSkill`/`expect`, `completeTask`, язык выражений `$.task…`);
ADR-0018 (gate-approval); ADR-0048 (lifecycle версии типа иммутабелен);
ADR-0056 (вызов Skill ядром через `skill_invocation`); TAI-ADR-0035 п.4
(verification — стадия, а не статус); конституция 1.0.0, ст. II и IV.

## Контекст

У задачи можно объявить критерии приёмки (ADR-0062), но ядро их не
исполняет: задача становится выполненной сразу по завершении, какие бы
критерии у неё ни были. Метрика «доля работы, доведённой до проверенного
результата» поэтому всегда равна нулю, а «готово» по-прежнему означает
«кто-то нажал complete». Закрыть задачу как выполненную можно тремя путями —
`POST /tasks/{ref}:complete`, `POST /runs/{id}:succeed` и исход approval
`completeTask`; все три сходятся в `finish_locked_task`.

Механизм должен быть общим для любого домена: ядро знает задачи, критерии,
скиллы, наблюдения, решения людей и правила, но не знает, что проверяется, —
сборка, оплата счёта или подписанный договор. Что и как проверять, приходит
данными пакета (конституция ст. II, FR-013). Исполнитель — человек или
машина — для стадии неразличим.

## Решение

### 1. Стадия, а не статус

У задачи с **непустым** `acceptance` любой из трёх путей завершения снимает
claim, но **не** ставит `completionStatus`: задача остаётся в текущем статусе
и получает открытую **попытку проверки**. Пока попытка открыта, задача не
берётся исполнителями — причина `verification_pending` в claimability
(`GET /tasks/{ref}/claimability`, рядом с `approval_required`). Успех попытки
переводит задачу в `completionStatus` и пишет `task.completed` и
`task.verified` **в одной транзакции**; обходного пути к «выполнено» нет —
исход approval, закрывающий задачу, проходит ту же стадию.

У задачи **без** acceptance поведение прежнее: `:complete` сразу выполняет
её, попытки нет (FR-005).

Отвергнуто: новая системная категория `verification` (ломает lifecycle всех
тенантов: версия типа иммутабельна и принадлежит тенанту); «ставить done и
проверять после» (задача с критериями оказалась бы выполненной без
пройденной проверки — SC-005 и метрика врут).

### 2. Попытка

Попытка — одно исполнение всех критериев задачи после завершения работы.
Таблица `task_verifications` (миграция — в T002): `attempt` (уникален с
`task_id`, `max + 1`), `status` `running | waiting_human | waiting_external |
passed | failed | cancelled`, `trigger` `run | complete | approval | rule` и
`trigger_ref`, `authority_principal_id`, `results` —
`[{key, kind, status, evidence, reason}]` по каждому критерию, `cursor`
(индекс текущего критерия), `skill_invocation_id`, `approval_id`,
`next_check_at`, `started_at`, `finished_at`. Частичный уникальный индекс —
**одна незавершённая попытка на задачу**: повторное завершение, пока попытка
идёт, новой не создаёт (FR-011). Прежние попытки остаются в истории.

Исполняет попытку worker — проход `process_verifications()` в `run_once` по
образцу очередей исходов approval: без нового процесса и без блокировки
worker'а (ожидание — `next_check_at`). Проверка идёт **полномочиями
principal'а, завершившего задачу** (как исход approval — полномочиями
решившего).

### 3. Порядок критериев

Критерии исполняются **в порядке объявления** в `acceptance`. Первый
проваленный останавливает попытку (остальные не исполняются), ожидающий
(`human`, `external_state`) приостанавливает её до решения или факта.
Жёсткой сортировки по виду нет: автор знает лучше, а TAI-ADR-0020 и
TAI-ADR-0035 расходятся в порядке. Ядро только **советует** порядок
`deterministic → external_state → human/llm_judge`
(`check_order_advice` в `domain/work_graph.py` — предупреждение, не отказ;
его показывает проверка пакета).

### 4. Грамматика `acceptance[].spec` по видам

Форма критерия ADR-0062 `{key, kind, description, spec?}` сохраняется;
`spec` задачи теперь проверяется по грамматике своего вида
(`check_spec` в `domain/work_graph.py`, вызывается из `normalize_checks`).
Проверка — **при записи** (`POST`/`PATCH /tasks`, будущие `ensure_work`
правил и `ensureWork` исходов), а не при исполнении: критерий, который
стадия не сможет исполнить, отвергается сразу.

| kind | `spec` | когда пройден |
|---|---|---|
| `deterministic` | `{skill: "name@version", inputs?: {…}, expect?: {поле: литерал}}` | вызов скилла закончился `succeeded` и каждое поле `expect` равно выходу (без `expect` — сам успех вызова) |
| `external_state` | `{}` или `{event: "<вид наблюдения или тип события>"}` | в `evidence` задачи есть элемент с `check: <key>` (при `event` — указывающий на факт этого типа) |
| `human` | `{}`, `{approver: <id principal'а>}` или `{approverRole: <id роли>}` | gate-approval задачи решён `approved` (п.6) |
| `llm_judge` | как `human` плюс `rubric?` (текст ≤ 2000) | как `human` (п.6) |

- `deterministic` — **тот же язык**, что `invokeSkill`/`expect` исходов
  approval (ADR-0061), а не второй: ссылка на скилл только закреплённая
  `name@version` (проверка не меняется под опубликованной задачей); строки
  `inputs` — выражения и шаблоны ADR-0061 п.3, но читают **только** `$.task…`
  (`$.approval`, `$.spawnedBy`, `$.invocation` у критерия нет); `expect` —
  имена полей выхода и литералы (строка, число, булево, `null`), без
  выражений.
- `approver` / `approverRole` — UUID (как `fields.approver` /
  `fields.approverRole` у `request_decision` правил, ADR-0063 п.5), взаимно
  исключают друг друга.
- Существование скилла и его `sideEffects` — вопрос реестра, а не формы:
  их проверяет прикладной слой при записи (T002). Скилл с
  `sideEffects: external_write` в критерии запрещён — у проверки нет
  approval-основания внешней записи (ADR-0056 §4).
- Неверный `spec` любого вида — **`422 invalid_acceptance_spec`**,
  `details: {field: <путь>, kind: <вид>}`. Размер (≤ 16 KiB,
  `invalid_document` / `payload_too_large`) и секреты
  (`secret_material_rejected`) проверяются раньше грамматики, как и прежде.
- **Совместимость.** Критерий без `spec` принимается, как раньше, любого вида.
  Исполняется он по умолчаниям вида: `external_state`, `human`, `llm_judge` —
  как с `{}`; `deterministic` без `spec` скилла не имеет, и стадия засчитывает
  его так же, как `external_state` — по evidence с `check: <key>`. Сохранённые
  раньше `spec` не перепроверяются, пока acceptance не заменяют (PATCH
  проверяет список целиком).
- **Критерии цели** (`goals.criteria`) — та же форма, но `spec` там
  по-прежнему не интерпретируется (`typed_spec=False`): цели никто не
  проверяет, контроллера целей нет (решение владельца 2026-09-25).

Отвергнуто: форма `{kind, ref, params}` из TAI-ADR-0035 — расходится с
реализованной в M1.1 формой ADR-0062; TAI-ADR-0035 обновляется ссылкой на это
решение (T007).

### 5. Провал — возврат исполнителю, лимит попыток

Проваленная попытка (`failed`) не закрывает задачу: событие
`task.verification_failed`, комментарий к задаче с причинами по критериям,
задача переходит в `releaseStatus` своего lifecycle и снова берётся
исполнителем на следующую попытку — без участия человека. **Третий провал
подряд** переводит задачу в первый статус категории `blocked`, достижимый по
её lifecycle, и она ждёт решения человека. Скилл, не давший результата за
`verification_skill_timeout_seconds` (по умолчанию 900 с), — провал критерия
с причиной `no_result`; внешний факт, не пришедший за
`verification_external_timeout_seconds`, — так же. Проверка не проходит
«молча».

Задача, отменённая во время попытки, закрывает попытку `cancelled`; живой
вызов скилла отзывается (как `approval_withdrawn` у исходов approval).
Успехом такая попытка не записывается.

Отвергнуто: отдельная задача на исправление (удваивает работу и связи,
конституция ст. VIII); бесконечные попытки.

### 6. `human` — решение gate-approval; `llm_judge` — как `human`

Отдельного эндпоинта `:verify` нет (решение владельца 2026-09-25):
подтверждение человека — решение approval, которое уже есть в цикле.
Критерий `human` пройден, если gate-approval задачи решён `approved` после
начала попытки, или если само решение approval привело к завершению (исход
`completeTask` — попытка с `trigger = approval` засчитывает этим approval
критерии `human` сразу). Evidence — `{kind: approval, ref}`. Если открытого
approval нет, попытка запрашивает его у `approver` / `approverRole` (иначе у
owner, затем assignee задачи — только если это человек, см. «Реализация
T004») и ждёт в `waiting_human`; решение пробуждает
её. Отклонение — провал критерия с комментарием решения в причине.

`llm_judge` в этой фазе **не исполняется моделью**: он требует решения
человека и ведёт себя как `human`, `rubric` показывается решающему.
LLM-судья ворота не закрывает (конституция ст. IV).

### 7. События

- `task.verification_started` — попытка открыта: `taskId`, `verificationId`,
  `attempt`, `trigger`, `checks` (число критериев).
- `task.verified` — попытка пройдена: те же поля плюс `results` —
  `[{key, kind, status}]` и указатели evidence (без текста причин, как
  ADR-0015); пишется в одной транзакции с `task.completed`.
- `task.verification_failed` — попытка провалена: плюс `failedCheck`,
  `reason` (код), `consecutiveFailures`, `blocked` (исчерпан ли лимит).

Все три — сущность `task`, корреляция — завершение, открывшее попытку.
Повторная доставка завершения или решения approval событий не дублирует
(SC-004).

### 8. Проекция и сводка

- `GET /api/v1/tasks/{ref}/verifications` — попытки задачи, новые первыми,
  под `tasks.read` (тот же AuthDep, что у задачи), курсорная пагинация.
- `TaskOut.verification` — сводка последней попытки `{status, attempt,
  updatedAt}` или `null`.
- Claimability — причина `verification_pending` с `verificationId` и
  `status` открытой попытки.
- SDK `list_task_verifications`, MCP `cp_get_task` показывает сводку.

### 9. Нейтральность

Модули стадии проверки (`commands/verification.py`, грамматика в
`domain/work_graph.py`) знают только задачи, критерии, скиллы, наблюдения,
evidence и approvals. В них нет понятий конкретного домена — ни кода,
репозиториев, коммитов и веток, ни счетов, платежей и договоров — и нет
ветвлений по типу задачи или пакету. Тесты ядра называют скиллы и события
нейтрально (`check.sample`, `event.sample`). Нейтральность доказывается
тестом: пакет-фикстура второго домена (T005,
`tests/fixtures/packages/invoice-payment/`, только данные) проходит сценарии
стадии на том же ядре без единой строки кода под него (SC-007), — и пробой
`absent` ниже.

## Реализация T002 (TASK-000393)

- `commands/verification.py` — открытие попытки (`open_verification` из
  `finish_locked_task`, общего для трёх путей; `trigger` — `complete`, `run`
  с `triggerRef` = id run, `approval` с id решения), проход worker'а
  (`process_verifications` → `execute_verification`: `SKIP LOCKED` по строке
  задачи, затем попытка), отмена (`cancel_open_attempt` из `update_task` при
  переходе в `terminal_cancelled` — PATCH и `cancel_work` правил). Переход в
  `completionStatus` и `task.completed` — одна функция
  `mark_task_completed` для задачи без критериев и для пройденной попытки.
- Кроме колонок п.2 попытка хранит `checks` — acceptance на момент открытия
  (PATCH acceptance во время попытки не меняет исполняемое), `authority` —
  снимок credential завершившего (как `decision_authority` у approval; в API
  не отдаётся, на каждом проходе проверяется, что credential ещё активен) и
  `correlation_id` завершения — его несут все события попытки.
- Реестр при записи: скилл критерия `deterministic` должен быть
  зарегистрирован и не `external_write`, иначе `422 invalid_acceptance_spec`
  с `details.field = acceptance[i].spec.skill`.
- Вызов скилла — `invoke_skill` с `requested_by = (verification, <id
  попытки>)`, ключ идемпотентности `verification:<id попытки>:<индекс>`,
  привязан к задаче (права `skills.invoke` и `tasks.write` — завершившего).
  Таймаут считается от постановки вызова; просроченный вызов отменяется
  системой. Отказ самого вызова (нет права, скилл выключен, входы не по
  схеме) — провал критерия с кодом ошибки в `reason`.
- `external_state` (и `deterministic` без `spec`) исполняется уже здесь:
  evidence с `check: <key>` (при `event` — наблюдение этого `kind`), ожидание
  `waiting_external` до `verification_external_timeout_seconds`. `human` и
  `llm_judge` в T002 только ставят попытку в `waiting_human` без таймера —
  запрос и решение approval добавляет T003.
- «Подряд» — провалы от последней непроваленной попытки; `cancelled` серию
  не прерывает и не продолжает. После исчерпания лимита каждый следующий
  провал снова переводит задачу в `blocked`. Категория `blocked` по-прежнему
  берётся исполнителями (как до v0.8): «ждёт человека» держится на статусе,
  а не на запрете claim.
- Отдельного события отмены попытки нет: её видно в `task.updated` (переход в
  отменённый статус) и в `GET /tasks/{ref}/verifications`.

## Реализация T003 (TASK-000394)

- `_decision_check` в `commands/verification.py` — для `human` и
  `llm_judge` одинаково. Засчитывается, по порядку: approval триггера
  (`trigger = approval`, `triggerRef` — gate этой задачи, решён `approved`) —
  **всем** таким критериям попытки; затем gate задачи, решённый `approved`
  после `started_at` попытки и ещё не процитированный её результатами, —
  одному критерию (каждому критерию без триггера — своё решение: у них
  могут быть разные `approver`).
- Иначе попытка ждёт первый открытый gate задачи, а если его нет —
  запрашивает gate-approval (`request_approval`, `gate = true`, workspace
  задачи) у `approver` / `approverRole`, иначе у owner, затем assignee (с
  T004 — только principal вида `human`).
  Никого нет — провал критерия `no_approver`; отказ самого запроса — провал
  с кодом ошибки. Текст запроса — ключ, вид, описание критерия и номер
  попытки; у `llm_judge` — ещё `rubric`. Ожидаемый approval — в
  `approval_id` попытки (сбрасывается при переходе к следующему критерию,
  как `skill_invocation_id`).
- **Запрос подаёт ядро**, а не завершивший: принципал ядра, суженный до
  `approvals.manage`, как комментарий о провале (`tasks.write`). Типичный
  завершивший — раннер без права просить решения; без этого `human` у задач
  раннеров проваливался бы всегда. Решает по-прежнему только адресат
  (назначенный principal или держатель роли — обычная проверка
  `decide_approval`); сам критерий исполняется полномочиями завершившего, как
  и прежде (активность credential проверяется на каждом проходе).
- Пробуждение: `decide_approval` и `cancel_approval` gate-approval задачи
  ставят `next_check_at = now` её попытке в `waiting_human`
  (`wake_on_decision`, обычный `UPDATE`: если worker держит попытку, он
  дождётся его коммита и увидит оставленный статус — решение не
  теряется). Таймера у `waiting_human` нет.
- `approved` — `passed`, evidence `{kind: approval, ref}`; `rejected` —
  провал `approval_rejected`, `cancelled` — `approval_cancelled`; комментарий
  approval — в `message` и в комментарий к задаче.
- Попытка, закрытая провалом или отменой задачи, отменяет **свой**
  незакрытый запрос (иначе открытый gate держал бы задачу); чужой gate,
  которого она только ждала, не трогается.
- Пока ожидаемый gate открыт, повторное завершение задачи — `409
  approval_required` (проверка gate идёт раньше, чем «попытка уже открыта»).
- Исход approval `completeTask` на gate, запрошенном самой попыткой, находит
  открытую попытку и ничего не делает (FR-011); задачу закрывает попытка.

## Реализация T004 (TASK-000395)

- **Попытка правила.** `complete_work` (ADR-0063, амендмент А1) открывает
  попытку `trigger = rule`, `triggerRef = rule_evaluation:<id>` тем же
  `finish_locked_task`; у задачи без acceptance попытка исполняет неявный
  критерий `{key: rule-evidence, kind: external_state}` (`checks` попытки —
  он, а не пустой acceptance). Повторное завершение, пока попытка открыта, —
  у любой задачи, не только с acceptance, — ничего не пишет (FR-011).
- **Evidence будит попытку.** Изменение evidence задачи ставит
  `next_check_at = now` её попытке в `waiting_external` (`wake_on_evidence`,
  из `update_task`): факт, записанный правилом или человеком, не ждёт
  следующего опроса.
- **Запасной решающий — только человек.** Без `approver` / `approverRole`
  попытка просит решения у owner задачи, затем у assignee — если это
  principal вида `human` (`_person_to_ask`). Агент — например, раннер,
  исполнивший задачу, — не получает approval на приёмку собственной работы;
  людей нет — провал `no_approver`, approval не заводится.

## Реализация T005 (TASK-000396)

- **Пакет.** `tests/fixtures/packages/invoice-payment/` — только данные в
  формате пакетов суперпроекта (TAI-ADR-0044): тип `invoice-payment`
  («оплатить счёт», lifecycle `to_pay → paying → paid | withdrawn`, `on_hold`
  — категория `blocked`), роль `finance-director`, скилл
  `invoice.amount_match@1` (`local`, реализация — тестовая заглушка
  `tests/skill_stubs/amount_match.py`, суммы — десятичные строки) и три
  правила: `invoice-received` (`ensure_work` с тремя критериями —
  `amount-matches` `deterministic`, `payment-settled` `external_state` с
  `event: invoice.payment_settled`, `director-approved` `human` с
  `approverRole`), `invoice-withdrawn` (`cancel_work`) и `payment-settled`
  (`complete_work`, `check: payment-settled`). В поставку не входит.
- **Установка.** `tests/catalog_packages.py` создаёт объекты пакета только
  через публичный API (`POST /roles`, `/skills`, `/task-types`, `/rules`,
  `spec` — тело запроса как есть, `${ПЕРЕМЕННАЯ}` — переменная установки).
  Id роли в `approverRole` приходит переменной `${FINANCE_DIRECTOR_ROLE_ID}`:
  грамматика `human` принимает UUID, а ссылки по ключу роли у критерия нет.
- **Тест.** `tests/integration/test_invoice_payment_package.py` проходит на
  пакете сценарии 1–5: сумма, факт банка и решение директора исполняются по
  порядку, задача `paid` только после всех трёх; неверная сумма и отказ
  директора возвращают работу исполнителю (агенту или человеку — стадия их не
  различает); отзыв счёта отменяет работу с открытой попыткой и работу под
  живым run (после остановки исполнителя); в том же tenant нейтральная копия
  пары правил другого домена (`sample-red`/`sample-green`, как `ci-red`/
  `ci-green` пакета selfdev) закрывает свою работу, не трогая чужую.
  Кода ядра под пакет не понадобилось — это и есть проверка SC-007; проба
  `absent` по `src/**/*.py` держит ядро без слов этого домена.

## Границы

- LLM-судья не исполняется; критериев по умолчанию у типа задачи нет
  (acceptance задаёт автор задачи, правило или исход approval).
- Контроллера целей нет: цель не переводится в `achieved` автоматически.
- Условия правил и критерии не читают факты памяти.
- Отдельного ручного подтверждения (`:verify`) нет — только approval.

## Последствия

- «Выполнено» у задачи с критериями означает «критерии пройдены»: SC-005
  проверяется тестами трёх путей завершения (T002).
- Прежние задачи и клиенты без acceptance не замечают стадии; критерий со
  `spec`, который раньше принимался молча (например, `{suite: …}` у
  `deterministic`), теперь — `422 invalid_acceptance_spec`.
- Worker получает четвёртый вид работы — проход проверок.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/domain/work_graph.py, pattern: 'INVALID_ACCEPTANCE_SPEC = "invalid_acceptance_spec"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/work_graph.py, pattern: 'def check_spec\('}
  repo: control-plane
- grep: {path: tests/unit/test_work_graph_domain.py, pattern: 'test_each_kind_refuses_a_spec_outside_its_grammar'}
  repo: control-plane
- grep: {path: tests/unit/test_work_graph_domain.py, pattern: 'test_a_check_without_spec_is_accepted_as_before'}
  repo: control-plane
- absent: {path: src/control_plane/domain/work_graph.py, pattern: '(?i)\bcommit|\bbranch|repositor|invoice|payment|contract\b'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "task_verifications"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/**/*.py", pattern: 'event_type="task\.verified"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/**/*.py", pattern: 'event_type="task\.verification_failed"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/tasks/\{[a-z_]+\}/verifications"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/discovery.py, pattern: '"verification_pending"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/verification.py, pattern: 'async def _decision_check\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'await wake_on_decision\(session, approval\)'}
  repo: control-plane
- grep: {path: tests/integration/test_verification_m16.py, pattern: 'test_an_approval_that_completes_the_task_counts_for_its_human_checks'}
  repo: control-plane
- grep: {path: tests/integration/test_verification_m16.py, pattern: 'test_an_agent_executor_is_never_asked_to_accept_its_own_work'}
  repo: control-plane
- grep: {path: tests/integration/test_verification_m16.py, pattern: 'test_completing_again_while_the_attempt_is_open_changes_nothing'}
  repo: control-plane
- absent: {path: src/control_plane/application/commands/verification.py, pattern: '(?i)\bcommit|\bbranch|repositor|invoice|payment|contract\b'}
  repo: control-plane
- absent: {path: src/control_plane/domain/work_rules.py, pattern: '(?i)\bcommit|\bbranch|repositor|invoice|payment|contract\b'}
  repo: control-plane
- absent: {path: "src/**/*.py", pattern: '(?i)invoice|payment'}
  repo: control-plane
- grep: {path: tests/fixtures/packages/invoice-payment/rules/payment-settled.yaml, pattern: 'kind: complete_work'}
  repo: control-plane
- grep: {path: tests/integration/test_invoice_payment_package.py, pattern: 'test_a_paid_invoice_is_done_only_after_the_bank_and_the_director'}
  repo: control-plane
- grep: {path: tests/integration/test_invoice_payment_package.py, pattern: 'test_two_packages_in_one_tenant_do_not_touch_each_others_work'}
  repo: control-plane
```
