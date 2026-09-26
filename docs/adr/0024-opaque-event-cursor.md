# ADR-0024: Непрозрачный публичный EventCursor и миграция с integer

Статус: Принято (v0.4)

## Контекст

v0.3 экспонировала `sequence` как публичный курсор (`after=<int>`,
`eventCursor: int`). Клиенты сортировали и арифметизировали его, что
зашивало ordering internals в клиентский код и делало исправление ADR-0023
невозможным без смены контракта.

## Решение

Публичный курсор — непрозрачная версионированная строка:
`ec1_<base64url(JSON)>`, payload `{"t": tx_id, "s": sequence}` (позиция)
или `{"q": floor}` (legacy-floor). Свойства: URL-safe, без секретов,
версионируется префиксом (`ec2_...` → `422 unsupported_cursor_version`),
структурно валидируется (`invalid_cursor`). Клиенты обязаны хранить и
возвращать курсор verbatim — не сравнивать, не конструировать.

API `/events`: ответ всегда несёт `nextCursor` (echo при пустой странице) и
`hasMore`; каждое событие несёт своё поле `cursor` (для WS-потока). WS
`?after=` принимает opaque-курсор.

Миграция legacy-входов (окно совместимости):

- `after=<int>` и старый `nextCursor` (`base64url {"s": int}`) декодируются
  в legacy-floor: одна протяжка фильтра `sequence > floor` в новом порядке;
  первое выданное событие поднимает курсор до полноценной позиции.
- Семантика переключения — at-least-once: события, которые legacy-клиент
  уже видел, могут прийти повторно (включая события, которые v0.3-дефект
  для него потерял). Потерь при переключении нет; ретроактивно восстановить
  события, потерянные ДО миграции конкретным клиентом, невозможно —
  задокументировано.

Harness Protocol: bump до **v2** (`control-harness/2`) — тип `eventCursor`
в context-ответах сменился int→string, это wire-breaking для v1-клиентов,
и косметикой это не объявишь. Сервер принимает открытие сессий v1 и v2;
ответы форматируются по v2 (v1-клиенты, парсившие int, должны обновиться —
задокументировано в harness-protocol.md).

`sequence` остаётся в ответах как идентификатор/audit/диагностика — явно НЕ
курсор.

## Последствия

Ordering internals инкапсулированы: будущая смена representation (например,
commit-order primitive) не потребует новой клиентской миграции.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/event_cursor.py, pattern: '_PREFIX = f"ec\{CURSOR_VERSION\}_"'}
  repo: control-plane
- grep: {path: src/control_plane/application/event_cursor.py, pattern: '"unsupported_cursor_version"'}
  repo: control-plane
- grep: {path: src/control_plane/application/event_cursor.py, pattern: 'def encode_legacy_floor\('}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/schemas.py, pattern: 'has_more: bool'}
  repo: control-plane
- grep: {path: src/control_plane/domain/enums.py, pattern: 'SUPPORTED_HARNESS_PROTOCOL_VERSIONS = frozenset\(\{"1", "2"\}\)'}
  repo: control-plane
```
