# Эксплуатационный runbook

Документ для дежурного инженера. Всё, что здесь описано, выполняется через
API и CLI — прямых правок в базе не требуется ни в одном сценарии.

## Что смотреть в первую очередь

```bash
curl -s localhost:8000/health/ready     # 503 = БД недоступна ИЛИ миграции отстали
curl -s localhost:8000/metrics | grep context_adapter
control-plane ops adapter status        # диагностика доставки этого Tenant
```

Ключевые сигналы `/metrics`:

| Метрика | Что означает |
|---|---|
| `context_adapter_parked_tenants` | > 0 — есть Tenant, доставка которого встала; остальные при этом работают |
| `context_adapter_lag` | отставание журнала (cap 1000); `context_adapter_lag_capped 1` — отставание больше cap |
| `context_adapter_failures_total` | суммарные отказы провайдера по всем Tenant |
| `active_claims`, `active_runs` | живая работа; резкий обвал вместе с `sessions` — обычно потеря сети до БД |
| `stale_fencing_rejections_total` | зомби-harness пытается писать после takeover — ожидаемо, но всплеск стоит посмотреть |

Логи структурированные: коррелируйте по `request_id` (один HTTP-запрос) и
`run_id` (сквозная трасса `X-Run-Id`, ADR-0039). `run_id` виден и на стороне
Memory Service.

## Сценарий: доставка в память встала (parked)

Симптом: `context_adapter_parked_tenants > 0`, у `/api/v1/context`
`freshness.memoryIngest.status = "parked"`.

```bash
control-plane ops adapter status
# {"parked": true, "parkedReason": "...", "parkedEventId": "...", "cursor": "ec1_..."}
```

Порядок действий:

1. **Прочитать причину.** `parkedReason` — ответ провайдера. Типичные:
   невалидный `kind` наблюдения, отвергнутая схема, 401/403 из-за протухшего
   `CP_CONTEXT_API_KEY`.
2. **Устранить причину.** Ключ — переменная окружения адаптера; ошибка
   маппинга — код `application/context/mapping.py`; сбой провайдера — на
   стороне Memory Service.
3. **Повторить ту же позицию:**

```bash
control-plane ops adapter redrive <tenantId> --reason "provider key rotated"
```

Redrive **не двигает курсор** — адаптер повторит ровно то же событие.
Операция идемпотентна, пишет audit-событие `context_adapter.redriven` и
требует права `operations.manage`. API, способного «перепрыгнуть» событие, в
системе нет намеренно (ADR-0037): тихий пропуск ломает воспроизводимость
памяти.

Пока Tenant запаркован, остальные Tenant доставляются как обычно — это и есть
смысл per-tenant изоляции (ADR-0036). Координация (claims, runs, approvals,
completion) не зависит от памяти вообще.

## Сценарий: память рассинхронизирована, нужен rebuild

```bash
curl -s -X POST localhost:8000/api/v1/operations/context-adapter/<tenantId>:rebuild \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"reason": "memory restored from an older backup"}'
```

Без `cursor` курсор уезжает в origin и Tenant переигрывается целиком; Memory
дедуплицирует по `event:<uuid>`, поэтому повтор безопасен. С `cursor` можно
отмотать на конкретную позицию — **только назад**; попытка вперёд отклоняется
`422 cursor_must_not_advance`.

Если запрошенная позиция ниже физически удалённого архива — `422
cursor_below_journal_floor` с доступным `floorCursor`.

## Сценарий: журнал вырос

Retention — операторская команда, планировщика нет (ADR-0038).

```bash
# 1. Переносим подтверждённую историю в event_archive (replay остаётся целым)
curl -s -X POST localhost:8000/api/v1/operations/journal:archive \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"beforeSeconds": 2592000, "maxEvents": 50000}'

# 2. Только когда архив действительно не нужен — физическое удаление
curl -s -X POST localhost:8000/api/v1/operations/journal:prune \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"beforeSeconds": 7776000}'
```

Гарантии:

- горизонт ограничен **минимумом** по всем consumer-курсорам и самым старым
  недоставленным outbox-событием: то, что кому-то ещё нужно, не уедет;
- `archived: 0` при живом отставании — это не ошибка, а работающая защита;
- отсутствие consumer-курсоров вообще — `409 retention_blocked_by_consumer`;
- после `:archive` replay прозрачен: `GET /events` читает архив и продолжает
  в горячей таблице;
- после `:prune` курсор ниже floor получает `422 cursor_below_journal_floor` —
  машиночитаемо, а не тихий короткий ответ.

`:prune` — единственная операция в системе, после которой данные теряются.
Она пишет событие `event_journal.pruned` и требует `operations.manage`.

## Backup и restore

Бэкапится **вся база** — состояние, журнал, архив и курсоры лежат в одном
PostgreSQL и восстанавливаются согласованно:

```bash
pg_dump --format=custom --no-owner control_plane > cp-$(date +%F).dump
pg_restore --clean --if-exists --no-owner -d control_plane cp-2026-08-12.dump
```

Порядок восстановления:

1. Поднять базу из дампа.
2. `alembic upgrade head` (дамп мог быть снят на более старой ревизии).
3. Проверить `GET /health/ready` — он вернёт `503 migrations_pending`, пока
   ревизия отстаёт.
4. Запустить API и worker, затем `context-adapter`.

После восстановления из **старого** дампа память может опережать журнал.
Это безопасно: Memory дедуплицирует повторы, а курсоры восстановились вместе
с базой. Если память, наоборот, потеряна — `:rebuild` в origin.

Частичное восстановление одной таблицы не поддерживается: журнал, состояние и
курсоры связаны инвариантами (append-only, FK, позиции), и рассинхронизация
между ними — это как раз то, чего вся конструкция избегает.

## Обновление версии

Для v0.5 → v0.6 см. [migration-v0.6.md](migration-v0.6.md). Коротко: остановить
`context-adapter`, `alembic upgrade head`, поднять API/worker, проверить
readiness, поднять адаптер.

Даунгрейд: `alembic downgrade <revision>`. Проверен для v0.5 → v0.4 на
наполненной базе; per-tenant курсоры сворачиваются в один глобальный по
минимуму (консервативно — часть событий доставится повторно).

### Диагностика operator harness

- Проверяйте `control-plane --version`, project config и credential store; API
  key не должен быть в MCP config, arguments или логах.
- `cp_whoami`/`cp_context` открывают Session. В context human Principal должен
  иметь `controlLevel=human_operated`, а `harness.type` — фактический client
  identifier (`codex` или `claude-code`). Расхождение metadata не даёт прав и
  указывает только на ошибку конфигурации процесса.
- После ambiguous handoff повторите запрос с тем же `Idempotency-Key`. Новый
  ключ означает новое решение и на уже suspended Run будет отклонён.
- `stale_claim` после handoff ожидаем: старый process обязан остановить writes.
  Второй harness создаёт новый Claim/Run и проверяет handoff checkpoint через
  Run Context.
- Не включайте абсолютные target paths, transcript, terminal history или
  credentials в checkpoint/evidence; handoff validator отклоняет известные
  unsafe формы.

## Ограничения, о которых стоит знать заранее

- Индексы в миграциях строятся не `CONCURRENTLY` — нужен maintenance window.
- `/metrics` не аутентифицирован; закрывайте его на уровне деплоя.
- Context Adapter — один процесс (singleton через advisory lock). Per-tenant
  изоляция изолирует **отказы**, а не даёт параллелизм.
- Архив журнала лежит в той же базе: `:archive` уменьшает горячую таблицу и
  стоимость её индексов, но не общий размер тома. Внешнее объектное хранилище
  — следующий этап.
- Outbox-доставка — структурированный лог; исчерпавшая retry запись
  dead-letter'ится (`available_at` в далёком будущем) и удерживает горизонт
  retention до тех пор, пока её не разберут.
