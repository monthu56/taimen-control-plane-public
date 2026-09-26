# ADR-0005. Transactional outbox для доставки событий

Статус: принято (2026-08-11)

## Контекст

Событие должно попасть наружу (realtime, downstream) тогда и только тогда,
когда транзакция закоммичена. Прямая публикация из процесса (in-memory emitter,
немедленный push в брокер) теряет события при падении между commit и publish
или публикует события откаченных транзакций.

## Решение

Команда записывает `outbox`-строку в той же транзакции, что состояние и
событие. Отдельный worker читает недоставленные записи батчами через
`FOR UPDATE SKIP LOCKED`, доставляет (в MVP — структурированный лог как точка
расширения), помечает `delivered_at`; при ошибке — bounded retry с
экспоненциальным backoff, после исчерпания попыток запись остаётся с
`last_error` для диагностики. Для мгновенного realtime внутри транзакции
дополнительно выполняется `pg_notify` — PostgreSQL доставляет его только при
commit, а потерю NOTIFY компенсирует polling по `sequence`.

## Обоснование

- At-least-once доставка без брокера и без 2PC.
- Падение worker'а между доставкой и commit — повторная доставка (идемпотентный
  consumer подразумевается контрактом: envelope несёт `eventId`/`sequence`).
- SKIP LOCKED позволяет запускать несколько worker'ов без координации.

## Последствия

- Доставка не exactly-once — потребители дедуплицируют по `eventId`.
- Мёртвые записи требуют ручного re-drive (сброс `attempt_count`) — приемлемо
  для MVP, инструментализация в v2.

## Амендмент 2026-09-25 (ADR-0068)

Конверт записи outbox дополнен полями `schemaVersion` (версия схемы `payload`
по каталогу событий) и `workspaceId` (workspace сущности события) — теми же,
что у события в журнале. Outbox остаётся точкой подключения будущего брокера
(TAI-ADR-0049); потребители сегодня читают журнал по курсору с фильтрами.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'postgresql_where=text\("delivered_at IS NULL"\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'last_error: Mapped\[str \| None\]'}
  repo: control-plane
- grep: {path: src/control_plane/worker/main.py, pattern: 'OutboxRecord\.attempt_count < self\.settings\.outbox_max_attempts'}
  repo: control-plane
- grep: {path: src/control_plane/worker/main.py, pattern: '\.with_for_update\(skip_locked=True\)'}
  repo: control-plane
- grep: {path: src/control_plane/worker/main.py, pattern: 'outbox_backoff_max_seconds'}
  repo: control-plane
```
