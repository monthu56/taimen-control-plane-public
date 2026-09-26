# ADR-0023: Надёжный replay-курсор — порядок (tx_id, sequence)

Статус: Принято (v0.4)

## Контекст

Подтверждённый дефект v0.3 (строгий xfail
`test_event_prefix_is_complete_under_xid_inversion`): `sequence`
присваивается при INSERT, `tx_id` — при первой записи транзакции; у двух
конкурентных команд порядки инвертируются, и sequence-курсор с фильтром
`tx_id < pg_snapshot_xmin` навсегда перешагивает событие ещё не
закоммиченной транзакции. Committed событие терялось для follower'ов.

## Решение

Публичный порядок доставки — пара **`(tx_id, sequence)`** под прежним
фильтром стабильного горизонта `tx_id < pg_snapshot_xmin(pg_current_snapshot())`.

Корректность (без предположений о commit-порядке, которых PostgreSQL не
даёт):

1. `tx_id` — `xid8` (64 бита, монотонный, без wrap-around), присваивается
   при первой записи транзакции.
2. Для снапшота читателя всё с `xid < xmin` уже завершилось: committed-строки
   видимы, aborted строк не оставили. Множество «события ниже горизонта»
   финально и растёт только событиями с `tx_id >= xmin`.
3. Любая незавершённая транзакция имеет `tx_id >= xmin`, т.е. её события
   сортируются строго ПОСЛЕ каждой позиции, выданной ниже горизонта. Курсор,
   продвигающийся только по выданным позициям, не может перешагнуть событие,
   которое закоммитится позже. Permanent gap невозможен.
4. `sequence` даёт детерминированный порядок внутри транзакции и полный
   tie-break.

Мы НЕ эмулируем commit-порядок (никаких `track_commit_timestamp`, logical
decoding, второй записи после коммита): фронтир по xmin достаточен и не
ломает инвариант «state + event + outbox в одной транзакции».

Цена: одна долгая открытая пишущая транзакция задерживает выдачу всех
событий с новее xid до своего завершения. Доставка задерживается, но не
теряется (команды — короткие одиночные транзакции; поведение
задокументировано и покрыто тестом).

Реализация: `ORDER BY tx_id, sequence` + row comparison
`(tx_id, sequence) > (:t, :s)`; индексы `(tenant_id, tx_id, sequence)` для
API-читателей и `(tx_id, sequence)` для глобального консюмера (adapter).
Историческим строкам backfill не нужен: `tx_id` заполнялся server default'ом
с v0.1.

## Последствия

Бывший xfail — обычный passing regression; набор конкуррентных тестов
(инверсия вставки, инверсия коммитов, 20 перемешанных писателей, пагинация,
WS-reconnect в окне инверсии, долгая транзакция) закрепляет гарантию.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'func\.pg_snapshot_xmin\(func\.pg_current_snapshot\(\)\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: '\.order_by\(Event\.tx_id\.asc\(\), Event\.sequence\.asc\(\)\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'tuple_\(Event\.tx_id, Event\.sequence\) > \(start\.tx_id, start\.sequence\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'Index\("ix_events_tenant_tx_sequence", "tenant_id", "tx_id", "sequence"\)'}
  repo: control-plane
- grep: {path: tests/integration/test_v03_review_fixes.py, pattern: 'async def test_event_prefix_is_complete_under_xid_inversion\('}
  repo: control-plane
- absent: {path: tests/integration/test_v03_review_fixes.py, pattern: 'xfail\(strict=True'}
  repo: control-plane
```
