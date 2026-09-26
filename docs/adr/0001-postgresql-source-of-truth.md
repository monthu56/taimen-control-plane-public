# ADR-0001. PostgreSQL — единственный источник истины

Статус: принято (2026-08-11)

## Контекст

Сервису нужны: строгие инварианты конкурентности (один активный claim на
задачу, optimistic versioning, fencing), аудит и realtime. Возможные хранилища:
реляционная БД, отдельный event store, комбинация БД + Redis.

## Решение

Единственное хранилище — PostgreSQL. Текущее состояние — нормализованные
таблицы; события — append-only таблица `events` в той же БД, записываемая в
одной транзакции с состоянием; очередь доставки — таблица `outbox` там же.

## Обоснование

- Все инварианты выражаются средствами PostgreSQL (row locks, частичные
  уникальные индексы, CHECK, FK, advisory locks) и не зависят от дисциплины в
  прикладном коде.
- Транзакционная атомарность «состояние + событие + outbox» без распределённых
  транзакций и двухфазных коммитов.
- LISTEN/NOTIFY даёт realtime-сигнал без дополнительного брокера.
- Одна система для бэкапа, восстановления и миграций.

## Последствия

- Вертикальный предел масштабирования одной БД; принимаем для MVP.
- Все компоненты (API, worker, WS-gateway) требуют доступа к одной БД.
- Смена БД не поддерживается (используются PostgreSQL-специфичные механизмы:
  JSONB, partial index, SKIP LOCKED, NOTIFY) — осознанно.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "events"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "outbox"'}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: 'OutboxRecord\('}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: 'func\.pg_notify\(NOTIFY_CHANNEL'}
  repo: control-plane
- absent: {path: pyproject.toml, pattern: '(?i)"(redis|celery|kafka|nats-py|eventstoredb)'}
  repo: control-plane
```
