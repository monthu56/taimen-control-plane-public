# ADR-0024: Непрозрачный публичный EventCursor и миграция с integer

Статус: Принято (v0.4); амендмент 2026-09-29 (runtime-console, TASK-000861) —
чтение журнала назад: `before`, `prevCursor`, `order`

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

## Амендмент 2026-09-29: чтение журнала назад (TASK-000861)

Контекст: экран журнала консоли (R008, TASK-000820) открывается на хвосте
(`?tail=N`) и листается назад к началу. Курсор v0.4 умел только вперёд:
пройти журнал назад можно было, лишь прочитав его целиком с начала.
Решение владельца 2026-09-29 — листание назад в ядре.

Решение:

1. **`GET /events?before=<cursor>&limit=N`** — до `N` событий строго до
   позиции курсора (тот же непрозрачный `ec1_…`), ближайших к нему. Внутри
   страницы события, как и везде, в порядке доставки `(tx_id, sequence)`.
   Всё строго ниже выданной позиции уже окончательно (горизонт стабильности,
   ADR-0023): при чтении назад событие не может «доехать» позже, поэтому
   обход назад не пропускает и не повторяет событий. Обход идёт через
   горячий журнал, затем архив (ADR-0038) — с той же перепроверкой floor, что
   у чтения вперёд.
2. **`prevCursor`** в ответе — значение `before` для предыдущей страницы.
   - Чтение назад (`before`, `tail`) знает точно: `prevCursor` — курсор
     самого старого события страницы, если до него что-то есть, и `null` на
     начале журнала (с учётом фильтров). У страницы `before` `hasMore`
     смотрит в ту же сторону, назад: `hasMore == (prevCursor != null)`.
   - У страницы вперёд (`cursor`/`after`/без курсора) `prevCursor` — курсор
     её первого события без проверки, есть ли что-то раньше (страница назад
     от него может оказаться пустой); у пустой страницы — `null`.
   - `nextCursor` страницы `before` — её самое новое событие: чтение вперёд
     от него доходит до событий, которые у клиента уже есть. Пустая страница
     возвращает `before` эхом — как пустая страница вперёд.
   - `tail` — это чтение назад от горизонта: теперь он добирает события из
     архива, если в горячем журнале их меньше `N`, и отдаёт `prevCursor`.
     Его `nextCursor` и `hasMore = false` не изменились.
   Листание консоли: `tail=N` → `before=prevCursor` → … до `prevCursor ==
   null`; склейка страниц даёт журнал с начала без пропусков и дублей (тест
   `test_walk_back_from_tail_reaches_the_start_without_gaps_or_repeats`).
3. **`order=asc|desc`** (по умолчанию `asc`) — только порядок событий
   внутри страницы: `desc` отдаёт их от новых к старым. Какие события
   попали на страницу и её курсоры от `order` не зависят. Иное значение —
   `400 invalid_request` (ADR-0058, как у прочих невалидных параметров).
4. **Направление одно.** `before` вместе с `cursor`, `after` или `tail` —
   `422 conflicting_cursors`, `details.parameters` называет переданные.
   `before` принимает только позиционный курсор: legacy-floor (`{"q": …}`,
   bare int, v0.3 `nextCursor`) — `422 invalid_cursor`.
5. **Фильтры и права — те же.** `types`, `workspaceId`, `entityType`,
   `entityId` сужают чтение назад так же, как вперёд (ADR-0068), `events.read`
   спрашивается так же (на workspace фильтра или на tenant). Курсор другого
   tenant'а не открывает его журнал: выборка всегда в tenant'е вызывающего.
6. **Ниже prune-floor** — как у чтения вперёд: `before` на позиции
   `archive_floor` или ниже — `422 cursor_below_journal_floor` с
   `details.floorCursor`. Пустая страница здесь читалась бы как «начало
   журнала», а события не кончились — их удалили.

WebSocket `/events/ws` не меняется: поток — только вперёд.

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
- grep: {path: src/control_plane/api/v1/schemas.py, pattern: 'prev_cursor: str \| None'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: '"conflicting_cursors"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/enums.py, pattern: 'SUPPORTED_HARNESS_PROTOCOL_VERSIONS = frozenset\(\{"1", "2"\}\)'}
  repo: control-plane
```
