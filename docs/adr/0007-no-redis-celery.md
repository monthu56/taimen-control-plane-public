# ADR-0007. Без Redis/Celery в первой версии

Статус: принято (2026-08-11)

## Контекст

Типовой рефлекс для фоновых задач и realtime — Redis (locks, pub/sub, очереди)
и Celery (workers). Их отсутствие — осознанное решение, а не упущение.

## Решение

Все роли, которые обычно отдают Redis/Celery, закрывает PostgreSQL:

| Потребность | Вместо Redis/Celery |
|---|---|
| Distributed lock | `SELECT ... FOR UPDATE` + lease/fencing в таблицах |
| Очередь фоновых задач | Таблица `outbox` + `FOR UPDATE SKIP LOCKED` |
| Pub/sub для realtime | `LISTEN/NOTIFY` + polling fallback |
| Кэш идемпотентности | Таблица `idempotency_keys` с TTL-очисткой |
| Периодика | Один worker-процесс с циклом |

## Обоснование

- Redis-локи без fencing небезопасны (проблема Redlock); наша схема даёт
  fencing бесплатно и транзакционно с состоянием.
- Каждая дополнительная система — это её HA, мониторинг, бэкап и ещё один
  источник несогласованности. Нагрузка MVP заведомо в пределах одной БД.
- Celery тянет брокер, сериализацию и собственную модель ретраев, дублирующую
  нашу outbox-логику, но без транзакционности с БД.

## Последствия

- При росте нагрузки outbox-паттерн переносится на брокер (Kafka/NATS) без
  изменения producer-стороны: контракт — таблица outbox.
- Троттлинг фоновой обработки ограничен возможностями одной БД — принимаем.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- absent: {path: pyproject.toml, pattern: '(?i)"(redis|celery|rq|dramatiq|kombu)[>=<~"\[]'}
  repo: control-plane
- absent: {path: "src/**/*.py", pattern: '^\s*(import|from)\s+(redis|celery|kombu|dramatiq)\b'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "idempotency_keys"'}
  repo: control-plane
- grep: {path: src/control_plane/worker/main.py, pattern: '\.with_for_update\(skip_locked=True\)'}
  repo: control-plane
```
