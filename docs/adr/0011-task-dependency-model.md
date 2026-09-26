# ADR-0011. Модель зависимостей задач и readiness

Статус: принято (2026-08-11, v0.2)

## Контекст

Нужен граф отношений задач и понятие готовности к работе — без workflow
engine, DAG-исполнителя и денормализованных флагов.

## Решение

Направленные рёбра `task_relations (from --type--> to)`:

| type | Семантика |
|---|---|
| `parent` | from — подзадача to (to — родитель) |
| `blocks` | from должна завершиться прежде, чем можно claim'ить to |
| `depends_on` | from нельзя claim'ить до завершения to |
| `spawned_by` | from порождена to |
| `related_to` | свободная ассоциация, без execution-семантики |

- Составные FK `(tenant_id, task_id)` — cross-tenant ребро невозможно на
  уровне БД. Self-relation и дубли запрещены constraint'ами.
- **Циклы**: blocking-рёбра нормализуются в граф «dependent needs
  prerequisite» (`depends_on`: from→to; `blocks`: to→from); вставка ребра
  X→Y отклоняется, если существует путь Y→*X (recursive CTE). Parent-иерархия
  проверяется тем же способом по своему графу. Вставки graph-структурных рёбер
  сериализуются per-tenant advisory lock'ом — два конкурентно-ацикличных
  ребра не могут сложиться в цикл.
- **Readiness вычисляется из authoritative state** в транзакции claim'а
  (никакой денормализации): задача claimable, если каждый prerequisite
  (`to` её `depends_on`-рёбер и `from` входящих `blocks`-рёбер) имеет статус
  `done`. Незакоммиченное завершение невидимо (READ COMMITTED) — dependent
  нельзя claim'ить до фактического commit'а завершения зависимости.
- Только `done` удовлетворяет зависимость: `cancelled` prerequisite продолжает
  блокировать, пока ребро не удалят (осознанно: отмена ≠ выполнение).
- Readiness — предикат момента claim'а, не инвариант времени жизни: reopen
  зависимости после захвата dependent'а не отменяет уже выданный claim.

## Последствия

- Никаких фоновых пересчётов и рассинхронизируемых флагов.
- Стоимость — один запрос по индексированным рёбрам в момент claim'а.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: "relation_type IN \\('parent', 'blocks', 'depends_on', 'spawned_by', 'related_to'\\)"}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/relations.py, pattern: 'async def _would_create_cycle\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/relations.py, pattern: 'pg_advisory_xact_lock\(func\.hashtextextended\(f"cp:taskgraph:'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/relations.py, pattern: "t\\.system_status_category != 'terminal_success'"}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'await check_task_readiness\(session, ctx, task\)'}
  repo: control-plane
```
