# CP-0054: Структурированная графовая память через Control Plane

Дата: 2026-09-12. Статус: Accepted.

## Решение

Расширить существующие `POST /observations`, `POST /context`, `cp_remember`
и `cp_get_context`. Отдельный MCP bridge или клиентский путь к Memory не нужен.
Control Plane принимает ограниченные `assertions` (entity/fact), записывает их
в журнал и передаёт через существующий Context Adapter. Автор, tenant,
source identity, provenance и namespace определяются сервером. Клиент может
указать `anchors` для поиска сущностей внутри разрешённого namespace;
это поисковые ключи, а не разрешения.

Запись требует `observations.write`, чтение — `events.read` текущего Principal;
обычные проверки task/run/project остаются обязательными. Без `events.read`
Memory не вызывается. Namespace всегда вычисляется из tenant в AuthContext.
Клиент не может задать scope, namespace, actor или provenance наблюдения.
Assertions не должны содержать управляющие поля Memory; произвольные свойства
сущности являются данными, а не политикой доступа.

## Граница действующей авторизации

В текущей модели `events.read` разрешает журнал всего tenant. WorkspaceMember
не является ACL журнала. Scopes и anchors повышают релевантность, но не
ограничивают доступ: обход графа может вернуть соседей вне поискового scope.
Это решение не обещает приватность между проектами одного tenant и не вводит
новую модель разрешений. Для более узких прав сначала нужен канонический
resource authorization в Control Plane, затем его обязательное применение
ко всем каналам Memory (observations, facts, graph, documents, summaries),
а не фильтрация уже сформированного ответа или локальные настройки агента.

## Контракт и последствия

Assertions валидируются до записи события; число и размер ограничены.
Поддерживаются entity (key, type, title, properties) и fact (subject,
predicate, object, confidence). Расширенные операции изменения истории
не открываются этим контрактом. Ключи имеют форму `type:id`.
Записи получают стандартную идемпотентность HTTP-команд; повторная доставка
того же события дедуплицируется Memory по event id. Новая команда является
новым наблюдением, даже если её текст совпал.

Гипотеза записывается как сущность с явным `epistemic_status=unverified`;
связь ABOUT не означает подтверждённый интерес клиента. Task/Run остаются
операционной истиной CP. PII-маскирование Memory не является ACL и не даёт
гарантии скрытия произвольных properties; в текущем tenant-wide режиме
Principal с `events.read` уже имеет доступ к исходному журналу.

Проверки: запрет записи/чтения без прав, неизменность серверных authority
полей, отклонение управляющих полей клиента, mapping assertions, anchors
в разрешённом namespace, совместимость текстового remember.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/context/assertions.py, pattern: 'extra="forbid"'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/schemas.py, pattern: 'assertions: list\[dict\[str, Any\]\]'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/schemas.py, pattern: 'anchors: list\[str\]'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/context.py, pattern: 'if not ctx\.has\(Permission\.EVENTS_READ\)'}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: 'assertions: list\[dict\[str, Any\]\] \| None'}
  repo: control-plane
- grep: {path: tests/integration/test_graph_memory.py, pattern: 'def test_client_cannot_set_observation_authority'}
  repo: control-plane
```
