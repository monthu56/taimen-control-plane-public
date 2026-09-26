# ADR-0012. Run: ownership и fencing-семантика

Статус: принято (2026-08-11, v0.2)

## Контекст

Claim — аренда владения задачей; нужна отдельная сущность для конкретной
попытки исполнения (attempt) с гарантией, что «зомби»-попытка не запишет
финальный результат после takeover.

## Решение

```
Task  = business intent
Claim = временное эксклюзивное владение (lease + fencing token)
Run   = конкретная попытка исполнения под claim'ом
```

- `Run` фиксирует `fencing_token` claim'а **на момент старта**; частичный
  уникальный индекс — не более одного `running` run на задачу; `attempt`
  монотонно нумеруется в рамках задачи (уникальный `(task_id, attempt)`).
- **start-run** идёт через claim gate под локом задачи: требуется живой claim
  и совпадение fencing token. Висящий `running` run прошлой эпохи при этом
  автоматически **supersede** (failed, reason `superseded`) — зеркально тому,
  как claim takeover реаппит истёкший claim.
- **:succeed** — единственный путь run'а записать финальный результат задачи.
  Под локом задачи повторно проверяется: run ещё `running`, его claim — всё
  ещё активный claim задачи, token равен `claim_epoch`, сессия держателя жива.
  По умолчанию `completeTask=true`: run.succeeded + claim released + task done
  происходят **в одной транзакции** — окна гонки между «run удался» и
  «задача завершена» не существует. `completeTask=false` оставляет задачу
  in_progress (следующий attempt возможен).
- **:fail / :cancel** финализируют только сам run (задачу не трогают), поэтому
  fencing не требуется — агент, потерявший lease, всегда может честно записать
  неудачу своей попытки. Разрешены владельцу run'а или `claims.manage`.
- Классический `POST /tasks/{id}:complete` при наличии `running` run текущего
  claim'а отвечает `409 run_in_progress` (завершайте через run); зомби-run
  прошлой эпохи он supersede'ит сам.
- Порядок блокировок расширен: session → task → claim → run.

## Последствия

- Старый fencing-протокол (PATCH/complete с claimId+token) сохраняется без
  изменений — runs дополняют его, не заменяя.
- История попыток неизменна и полна: superseded/failed runs остаются для аудита.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_runs_one_running_per_task"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("task_id", "attempt", name="uq_runs_task_attempt"\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/tasks.py, pattern: 'run\.failure_reason = "superseded"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/tasks.py, pattern: '"run_in_progress"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/runs.py, pattern: 'complete_task: bool = True'}
  repo: control-plane
- route: "POST /runs/{run_id}:succeed"
  repo: control-plane
```
