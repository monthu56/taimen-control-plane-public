# ADR-0038: Retention, архив и rebuild журнала событий

Статус: Принято (v0.5)

## Контекст

v0.4 зафиксировала: «журнал не имеет retention/archive policy» — он растёт
неограниченно. Просто удалять старые события нельзя: их могут ждать активные
consumer'ы, а replay и аудит — публичные гарантии.

Kafka и event sourcing запрещены требованиями. Физическое внешнее хранилище
(объектное) выходит за текущую инфраструктурную границу.

## Решение

DB-native фундамент из трёх частей.

### 1. Безопасный горизонт

Удалять можно только события, которые одновременно:

- старше `CP_JOURNAL_RETENTION_MIN_AGE_SECONDS` (по умолчанию 30 суток);
- строго ниже минимальной позиции по **всем** строкам
  `event_consumer_cursors` (то есть подтверждены всеми consumer'ами).

Если запрошенный горизонт нарушает второе условие, операция отклоняется
машиночитаемой ошибкой `409 retention_blocked_by_consumer` с указанием
блокирующего consumer'а и его позиции. Тихого удаления нужных событий не бывает.

### 2. Архив в той же базе

`POST /api/v1/operations/journal:archive` (`operations.manage`) переносит события
из `events` в `event_archive` (та же структура + `archived_at`) в одной
транзакции и продвигает `event_journal_floor` — глобальную запись
`(tx_id, sequence)` самой старой позиции, ещё присутствующей в `events`.

Replay прозрачен: `GET /events` с курсором ниже floor читает недостающий отрезок
из `event_archive` и продолжает из `events`. Аудит и replay-гарантии не
нарушаются — событие не исчезло, оно переехало.

`POST /api/v1/operations/journal:prune` удаляет строки **из архива** и поднимает
`archive_floor`. Только после этого события физически недоступны. Запрос курсора
ниже `archive_floor` возвращает `422 cursor_below_journal_floor` с
`details.floorCursor` — машиночитаемо, а не молчаливым пропуском.

### 3. Rebuild

`POST /api/v1/operations/context-adapter/{tenantId}:rebuild` (`operations.manage`)
переставляет курсор Tenant назад на переданный `cursor` (или в origin) и
разблокирует parked-состояние. Adapter переигрывает журнал (и архив) с этой
позиции; Memory дедуплицирует по `event:<uuid>`, поэтому повтор безопасен.
Курсор строго назад: попытка передвинуть вперёд отклоняется
`422 cursor_must_not_advance` — иначе rebuild стал бы обходом ADR-0037.
Запрос позиции ниже `archive_floor` возвращает ту же ошибку
`cursor_below_journal_floor`.

## Последствия

- Retention закрыт рабочим контрактом, а не только документацией: есть команды,
  ошибки, floor и тесты.
- Внешнее (объектное) хранилище архива **не** реализовано и честно объявлено
  следующим этапом: `event_archive` живёт в той же базе, поэтому `:archive`
  уменьшает горячую таблицу и стоимость индексов, но не общий размер тома.
- `:prune` — единственная операция, после которой данные действительно
  теряются; она требует `operations.manage`, пишет событие
  `event_journal.pruned` и в docker-compose не вызывается автоматически.
- Автоматического планировщика retention нет: это операторская команда. Cron —
  зона ответственности эксплуатации.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "event_archive"'}
  repo: control-plane
- route: "POST /journal:archive"
  repo: control-plane
- route: "POST /journal:prune"
  repo: control-plane
- route: "POST /context-adapter/{tenant_id}:rebuild"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/operations.py, pattern: '"retention_blocked_by_consumer"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: '"cursor_below_journal_floor"'}
  repo: control-plane
```
