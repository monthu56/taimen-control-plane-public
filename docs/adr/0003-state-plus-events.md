# ADR-0003. Состояние + неизменяемые события, а не event sourcing

Статус: принято (2026-08-11)

## Контекст

Нужен полный аудит изменений и realtime-поток, но также — простые запросы
текущего состояния и понятные инварианты конкурентности.

## Решение

Текущее состояние живёт в нормализованных таблицах и является первичным.
Каждая успешная мутация в той же транзакции добавляет доменное событие в
append-only журнал `events` (монотонный `sequence`, запрет UPDATE/DELETE
триггером БД). События служат для аудита, realtime, downstream-потребителей и
диагностики — не для восстановления состояния.

Heartbeat'ы сессий/claims события не порождают: это продление аренды с частотой
в секунды, а не доменное изменение; журналирование раздуло бы поток на порядки.
Факт продления виден в `heartbeat_at`/`expires_at` самой сущности.

## Обоснование

- Полный event sourcing заставил бы держать проекции и replay-механику ради
  сценария, которому они не нужны; SQL-инварианты (partial unique index,
  FOR UPDATE) работают только над materialized-состоянием.
- Транзакционная запись state+event даёт те же гарантии аудита без CQRS.

## Последствия

- Событий достаточно для построения проекций в будущем, но их replay не
  является механизмом восстановления.
- Журнал растёт неограниченно; архивация — забота v2.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: migrations/versions/1f5b0d9c44a3_initial_schema.py, pattern: 'CREATE TRIGGER events_append_only$'}
  repo: control-plane
- grep: {path: migrations/versions/1f5b0d9c44a3_initial_schema.py, pattern: 'BEFORE UPDATE OR DELETE ON events$'}
  repo: control-plane
- grep: {path: migrations/versions/1adf50721f1e_project_model_v05.py, pattern: "RAISE EXCEPTION 'events are append-only"}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: 'session\.flush\(\)\s+# populates event\.sequence'}
  repo: control-plane
- absent: {path: "src/control_plane/**/*.py", pattern: 'event_type="[a-z_]+\.heartbeat'}
  repo: control-plane
```
