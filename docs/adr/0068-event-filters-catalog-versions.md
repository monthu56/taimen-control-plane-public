# ADR-0068: Журнал событий как подписка — фильтры и права по workspace, каталог и версии данных, payload approvals v2, держатели роли

Статус: Accepted (2026-09-25), фича `notifications`, задача N002 (TASK-000411).
Spec/plan — `specs/notifications/` суперпроекта; дизайн одобрен владельцем в
TASK-000408. Платформенное решение — TAI-ADR-0049 «Событийная модель
платформы» (N001); этот ADR — его реализация в ядре.

Контекст: ADR-0003 (состояние + неизменяемые события), ADR-0005 (outbox —
точка будущего брокера, не меняется), ADR-0023/0024 (курсор журнала и порядок
`(tx_id, sequence)` под стабильным горизонтом), ADR-0038 (архив журнала),
ADR-0018 (approvals и право решать), ADR-0058 (неизвестный query-параметр —
400); конституция 1.0.0, ст. II (нейтральность) и V (контракт прежде
реализации).

## Контекст

Потребители событий ядра (сервис уведомлений, мост процессов, правила)
читают весь журнал tenant'а и сами отбрасывают лишнее. Для этого им нужно
право `events.read` на весь tenant, даже если их дело — approvals одного
workspace. Контракта данных события нет: какие поля в `payload` у
`approval.requested`, знает только код, и поменять их можно незаметно.
Событие `approval.requested` не говорит, что решается и кто просит, — каждый
потребитель дочитывает approval и задачу, с лишним кругом и гонкой с
отменой. Список адресатов решения по роли (кто вправе решить) снаружи
вычислить нельзя: правило видимости роли по дереву workspace живёт в ядре.

## Решение

1. **Подписка = фильтр чтения.** `GET /events` и `WS /events/ws` принимают
   `types` — префиксы типа (`approval.` — все события approvals,
   `task.verified` — этот тип; повторяемый параметр и/или через запятую, до 20;
   формат `^[a-z][a-z0-9_]*(\.[a-z0-9_]*)*$`, иначе `422
   invalid_event_type_filter`, в WS — код закрытия 4400) и `workspaceId` —
   события этого workspace и его потомков (поддерево вычисляется в момент
   чтения). Фильтры сочетаются с `entityType`/`entityId`, `cursor`, `tail`.
   Фильтр не меняет ни порядок, ни смысл курсора: отфильтрованный читатель
   продолжает с `nextCursor` так же, как нефильтрованный. Страница, дошедшая
   до конца журнала, переносит `nextCursor` через отброшенные фильтром события
   до позиции, стабильной до запроса (`past_filtered_out`), — читатель редкого
   типа не пересканирует один и тот же хвост при каждом опросе. Серверных
   подписок с состоянием нет: курсор хранит потребитель.
2. **Право — на workspace.** С `workspaceId` `events.read` спрашивается на
   ресурсе `workspace:<id>` (в режиме `policy` PDP учитывает гранты на
   предках — `authz/catalog.yaml` уже объявлял `events.read` на workspace);
   несуществующий workspace — 404 (WS — 4404). Без `workspaceId` — прежнее
   поведение: право на tenant. Отказ — 403 (WS — 4403). События уровня tenant
   (principal'ы, ключи, bootstrap) под фильтром по workspace не видны.
3. **Workspace события** — колонка `events.workspace_id` (и в
   `event_archive`), её заполняет `record_event` по сущности события:
   собственный workspace сущности (`task`, `approval`, `artifact`, `goal`,
   `rule`, `role`, `project`, сам `workspace`), иначе workspace задачи, к
   которой сущность относится (`run`, `claim`, `skill_invocation`, approval
   или артефакт без своего workspace). Остальные события — уровня tenant
   (`NULL`). Журнал append-only: у событий, записанных до этого решения,
   `workspace_id` пуст, и фильтр по workspace их не видит. В конверте события
   — поле `workspaceId`.
4. **Версия данных — `schemaVersion` в конверте** (колонка
   `events.schema_version`, у старых событий `1`). Реестр типов в коде —
   `domain/event_catalog.py`: тип → сущность → версии → JSON Schema `payload`.
   `record_event` ставит текущую версию типа и **отказывает в типе, которого
   нет в каталоге** (ошибка программирования, а не данных). Правило эволюции:
   версия только **добавляет** поля; поле, меняющее смысл или исчезающее, —
   новый тип. Старые версии остаются в каталоге, пока журнал может хранить
   события под ними. Экспорт в CloudEvents — функция SDK (N003,
   [ADR-0069](0069-event-consumer-sdk.md)), не формат ядра.
5. **Каталог публикуется из кода**: `make event-catalog` пишет
   `docs/events/catalog.md` и `docs/events/catalog.json` (JSON Schema всех
   версий); тест держит их в согласии с кодом. Каталог нейтрален — называет
   только сущности ядра.
6. **Payload approvals v2** (поля только добавлены):
   - `approval.requested` — `workspaceId` (approval'а; с амендмента
     2026-09-25 это и workspace его задачи),
     `taskPublicId`, `taskTitle`, `requestedBy`, `comment`, `gate` (был);
   - `approval.approved` / `approval.rejected` — `decisionBy`, `comment`
     (комментарий решения, `null` без него), `channel` (канал, через который
     пришло решение; `null` для прямого вызова API — заполняется из канала
     входа в N005, ADR-0070);
   - `approval.cancelled` — `cancelledBy`.
   Текст в событии (`comment`) проходит редакцию материала, похожего на
   секрет (`redact_secret_material`: тот же список, что у
   `reject_secret_text`, совпадение заменяется на `[redacted]`), и
   обрезается до 1000 символов. Полный текст остаётся в approval.
   Потребители v1 (правила, мост процессов, bidops) совместимы: схема v1
   принимает payload v2 (тест).
7. **Держатели роли**: `GET /roles/{id}/principals?workspaceId=` —
   principal'ы, у которых роль назначена на уровне tenant или на этом
   workspace / его предке; без `workspaceId` — только назначения уровня
   tenant. Это ровно правило права решать approval с `requiredRoleId`
   (`role_assignment_scope` — одна функция для обоих). Право — `org.read` или
   `principals.read` (уровень tenant, как у `GET /principals/{id}/roles`).
   Элемент — `{id, kind, displayName, status}`, страница — как у прочих
   списков; статус principal'а не фильтруется — решает адресующий.

## Contract-проверки

- После **каждого** интеграционного теста все события журнала проверяются по
  каталогу: известная пара (тип, версия) и `payload`, валидный по её JSON
  Schema (`tests/event_contract.py`). Весь интеграционный набор — contract-тест
  каталога. Тесты, пишущие события SQL-ом мимо ядра, помечены `raw_journal`.
- Статический тест: каждый строковый `event_type=` в `src/control_plane`
  есть в каталоге; динамические типы покрывает отказ `record_event`.
- Схемы — валидный JSON Schema 2020-12; версии только добавляют поля.

## Последствия

- Потребитель approvals одного workspace живёт с `events.read` на этом
  workspace, а не на tenant, и строит сообщение из события без дочитывания.
- Новый тип события без записи в каталог падает в первом же тесте, который
  его пишет; изменение `payload` без новой версии — в contract-проверке.
- Запись события стоит одного `flush` и, если сущность ещё не в identity map,
  одного-двух чтений по первичному ключу.
- Индекса под фильтры нет: они сужают упорядоченный скан так же, как
  `entityType`. Редкий фильтр на большом журнале сканирует до конца страницы;
  перенос курсора (п.1) не даёт повторять это на каждом опросе.
- Миграция `c5e1a7d3f9b2` добавляет две колонки без перезаписи таблицы;
  downgrade их удаляет (workspace новых событий и версии теряются, payload
  остаётся).

## Амендмент 2026-09-25: approval задачи живёт в workspace задачи (TASK-000443)

Найдено на staging (N009, TASK-000418): `request_approval` записывал в
approval только явно переданный `workspaceId`. Approval правила
`request_decision`, `cp_request_approval` раннера и харнесса, исход
`ensureWork.requestApproval` оставались без workspace, и решить их могли
только держатели роли уровня tenant. При этом `approval.requested` называл
workspace задачи, а `GET /roles/{id}/principals?workspaceId=` (п.7) — его
держателей: уведомление уходило тому, кто получал `403 not_eligible`.

Решение:

1. **Approval с задачей живёт в workspace задачи.** Без явного
   `workspaceId` `request_approval` ставит `approval.workspace_id =
   task.workspace_id` (у задачи уровня tenant — `NULL`, как прежде).
2. **Явный `workspaceId` может только расширить круг решающих**: он равен
   workspace задачи или является его предком, иначе `422 invalid_approval`.
   Workspace сбоку или ниже задачи отдал бы решение тем, кто задачу не видит;
   у задачи уровня tenant сузить некуда — любой явный workspace отказ.
   Approval без задачи — как прежде, workspace только явный.
3. `workspaceId` в `approval.requested`, workspace события в конверте (п.3) и
   `workspaceId` проекции approval — одно значение: workspace approval'а.
   Держатели роли по п.7 для него — ровно те, кто может решить.
4. **Миграция данных `d2f8b4a6e1c3`**: pending approvals с задачей и
   `workspace_id IS NULL` получают workspace задачи. Решённые и отменённые —
   история, не меняются; approval без задачи не меняются. Downgrade — no-op:
   заполненные строки неотличимы от созданных с workspace задачи и валидны
   для прежнего кода.

Следствие: роль, выданная на уровне tenant как обход (staging, invoice-payment,
finance-director), после выкатки не нужна — достаточно роли в workspace
задачи.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/events.py, pattern: 'schema_version = current_version\(event_type\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: 'workspace_id = await event_workspace\(session, entity_type, entity_id\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'Permission\.EVENTS_READ, resource=ResourceRef\("workspace"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'startswith\(p, autoescape=True\)'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"workspaceId, taskPublicId, taskTitle, requestedBy, comment"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'scope_filter = await role_assignment_scope\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'workspace_id = await _task_approval_workspace\(session, ctx, task, workspace_id\)'}
  repo: control-plane
- grep: {path: tests/integration/conftest.py, pattern: 'journal_violations\(sync_engine\)'}
  repo: control-plane
- grep: {path: tests/unit/test_event_catalog.py, pattern: 'def test_every_literal_type_in_the_core_is_in_the_catalog'}
  repo: control-plane
```
