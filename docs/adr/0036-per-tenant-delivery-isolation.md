# ADR-0036: Per-tenant изоляция доставки Context Adapter

Статус: Принято (v0.5)

## Контекст

v0.4 приняла ограничение: один глобальный курсор `context-adapter` означает, что
poison-событие одного Tenant останавливает доставку **всех** Tenant. Это
зафиксировано в baseline как эксплуатационный долг и должно быть закрыто в v0.5
без нарушения replayability и без внешнего брокера.

## Решение

Первичный ключ `event_consumer_cursors` становится составным
`(name, tenant_id)`. Курсор, parked-состояние, счётчик ошибок и backoff —
per-tenant. Добавлены типизированные колонки: `parked_at`, `parked_reason`,
`parked_event_id`, `failure_count`, `next_attempt_at`.

Цикл доставки:

1. выбрать Tenant-ов с непустым отставанием, исключив тех, у кого
   `next_attempt_at > now()`;
2. упорядочить по `updated_at` (самый давно не обслуженный — первым): это
   round-robin, который не даёт активному Tenant заморить остальных;
3. взять не более `CP_CONTEXT_MAX_TENANTS_PER_CYCLE` Tenant-ов за цикл и не более
   `CP_CONTEXT_TENANT_BATCH_SIZE` событий на Tenant;
4. доставить батч, подтвердить полностью, продвинуть курсор этого Tenant.

Отказ провайдера паркует **только** строку своего Tenant: пишутся
`parked_at`, `parked_reason`, `parked_event_id` и `next_attempt_at` с
экспоненциальным backoff. Курсор при этом не двигается — семантика
at-least-once и «никогда не перепрыгнуть событие» сохранена дословно.

Singleton по-прежнему обеспечивается session-level advisory lock: параллельная
обработка разных Tenant разными процессами в v0.5 не вводится, потому что
потребовала бы отдельного планировщика (запрещён требованиями). Изоляция здесь —
изоляция **отказов**, а не параллелизм.

### Replay-safe миграция

Старый глобальный курсор означает «все события с позицией ≤ G доставлены».
Миграция создаёт по строке на каждый существующий Tenant с той же позицией G —
это в точности эквивалентное утверждение, поэтому ни повторной доставки всего
журнала, ни пропуска не происходит. Tenant-ы, созданные после миграции,
начинают с origin `(0, 0)`, что корректно по определению.

Downgrade сворачивает per-tenant строки в одну глобальную с позицией
`min` по всем Tenant: консервативно (часть событий будет доставлена повторно),
но никогда не теряет.

## Последствия

- Poison одного Tenant больше не блокирует остальных — исходное требование
  закрыто.
- Один процесс по-прежнему обслуживает всех: пропускная способность не выросла,
  выросла отказоустойчивость. Горизонтальное шардирование adapter'ов — следующий
  этап.
- `/metrics` теперь агрегирует по строкам: `context_adapter_parked_tenants` —
  новый gauge; per-tenant метки намеренно не вводятся (кардинальность).
- Диагностика и redrive работают на этой же строке (ADR-0037).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "migrations/versions/*.py", pattern: '"pk_event_consumer_cursors", "event_consumer_cursors", \["name", "tenant_id"\]'}
  repo: control-plane
- grep: {path: src/control_plane/config.py, pattern: 'context_max_tenants_per_cycle: int'}
  repo: control-plane
- grep: {path: src/control_plane/config.py, pattern: 'context_tenant_batch_size: int'}
  repo: control-plane
- grep: {path: src/control_plane/worker/context_adapter.py, pattern: 'next_attempt_at IS NULL OR c\.next_attempt_at <= now\(\)'}
  repo: control-plane
- grep: {path: src/control_plane/main.py, pattern: 'context_adapter_parked_tenants'}
  repo: control-plane
```
