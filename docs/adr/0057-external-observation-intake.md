# ADR-0057: Приём внешних наблюдений: source, dedupKey, observedAt, supersedes, externalRef

Статус: Accepted (2026-09-23, M1.2a)

Контекст: TAI-ADR-0036 п.1 (контракт Observation для внешних наблюдений),
docs/pilots/self-development-2026-09.md §3 суперпроекта. Расширяет
[ADR-0027](0027-explicit-replayable-observations.md) и
[ADR-0054](0054-governed-graph-memory.md); их правила (одно событие журнала,
provenance из аутентификации, запрет скрытых рассуждений) не меняются.

## Контекст

`POST /api/v1/observations` принимал `kind`, `content`, `data`, `assertions` и
scope-ссылки. Этого хватает для «запомни находку», но не для наблюдений из
внешних систем (CI, трекер, мониторинг), которые опрашиваются повторно:

- неизвестно, **откуда** факт — система-источник лежала бы в свободном `data`;
- повторный опрос того же объекта создаёт новое наблюдение: `Idempotency-Key`
  защищает только транспортный повтор одного запроса одного principal'а
  (TTL ограничен), а не «тот же объект, увиденный снова»;
- время факта подменяется временем записи в журнал;
- нет способа сказать «это наблюдение заменяет предыдущее» (новое состояние
  того же issue/alert).

## Решение

### Новые поля запроса (все необязательны)

| Поле | Смысл |
|---|---|
| `source` | Система-источник: `^[a-z0-9][a-z0-9._:/-]{0,127}$`. Только нижний регистр: source — половина ключа дедупликации, `GitHub` и `github` не должны расщеплять один объект на два потока. Обязателен, если передан `dedupKey` или `externalRef`. |
| `dedupKey` | Ключ повтора внутри `source`, 1..512 символов, не пустой. |
| `observedAt` | Когда факт увиден во внешней системе; ISO 8601 **с часовым поясом** (naive — `400 invalid_request`). По умолчанию — время записи. |
| `supersedes` | id предыдущего наблюдения, которое заменяет это. |
| `externalRef` | `{system, id, url?}` — наблюдаемый объект в его системе. Лишние ключи — `400`. |

`source` здесь — метка внешней системы-источника, а не source identity в
Memory: identity доставки остаётся `control-plane/domain-events/event:<id>`
(ADR-0026), клиент её по-прежнему не задаёт (ADR-0054).

### Дедупликация: `(tenant, source, dedupKey)`

Пара `(source, dedupKey)` уникальна в пределах tenant'а. Первый запрос — `201`
и новое событие `observation.recorded`; любой следующий с той же парой — `200`
с **тем же** `id`, `eventId`, `kind`, `recordedAt` и без нового события, даже
если `content` отличается: это то же наблюдение того же объекта. Новое
состояние объекта — новый `dedupKey` (например, `issue-42@closed`) и
`supersedes` на прежнее наблюдение. В ответе добавлен `deduplicated: bool`,
чтобы клиент, не видящий HTTP-статус (MCP), различал случаи.

Хранение — таблица `observation_dedup_keys` с первичным ключом
`(tenant_id, source, dedup_key)` → `observation_id`, `event_id`, `kind`,
`recorded_at`. Это не rich-таблица наблюдений: authoritative запись остаётся
событием журнала (ADR-0027), таблица лишь помнит, какое наблюдение породил
ключ. Ключ занимается `INSERT … ON CONFLICT DO NOTHING` **до** записи события в
той же транзакции: параллельный двойник ждёт незакоммиченную строку
победителя и получает конфликт, поэтому на пару пишется ровно одно событие.
Запрос, отклонённый валидацией (включая `supersedes` → 404), ключ не занимает.
Без `dedupKey` поведение прежнее: каждый вызов — новое наблюдение.

### `supersedes`

Цель должна быть наблюдением (`observation.recorded`) **этого же** tenant'а в
журнале — в горячей таблице или в архиве (ADR-0038). Неизвестный или чужой
id — `404 not_found` до какой-либо записи, как и прочие scope-ссылки. Что цель
описывает тот же внешний объект, ядро не проверяет: связь заявляет клиент.

### Событие и mapping

`observation.recorded` несёт `source`, `dedupKey`, `supersedes`, `externalRef`
(если переданы) и всегда `observedAt`. Context mapping (версия 4) переносит
эти поля в `data` наблюдения Memory — серверно проверенные значения
перекрывают одноимённые ключи клиентского `data` — и использует `observedAt`
как `occurred_at`. События до этого решения `observedAt` не несут и
отображаются как раньше (`occurred_at` = время события).

### Клиенты

`control-plane-client` `remember(...)` и MCP `cp_remember` принимают `source`,
`dedup_key`, `observed_at`, `supersedes`, `external_ref`. Старые клиенты без
новых полей работают без изменений; единственное видимое отличие — поле
`deduplicated: false` в ответе и `observedAt` в payload события.

## Последствия

- Миграция `b3e7d1f9c2a4` создаёт `observation_dedup_keys`; данных не
  переносит. Ключи не истекают и не удаляются вместе с архивированием
  событий: повтор через год по-прежнему `200`. Downgrade удаляет таблицу, и
  повторный upgrade её из журнала не восстанавливает: первый повтор после
  такого отката создаст новое наблюдение (следующие снова дедуплицируются).
- Ошибки: нарушения правил полей — `422 observation_invalid`; неверные типы,
  naive `observedAt`, лишние ключи — `400 invalid_request`; `supersedes` —
  `404`.
- `Idempotency-Key` остаётся ортогональным: он воспроизводит сохранённый ответ
  запроса, дедупликация по `dedupKey` — свойство самого наблюдения.

## Conformance

Пробы для `adr.conformance_check`:

```conformance
- grep: {path: "src/control_plane/infrastructure/db/models.py", pattern: '__tablename__ = "observation_dedup_keys"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/schemas.py", pattern: 'dedup_key: str \| None'}
  repo: control-plane
- grep: {path: "src/control_plane/application/context/mapping.py", pattern: "_OBSERVATION_ORIGIN"}
  repo: control-plane
- grep: {path: "src/control_plane_mcp/server.py", pattern: "dedup_key=dedup_key"}
  repo: control-plane
```
