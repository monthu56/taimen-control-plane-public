# ADR-0018. Approval gate + suspension: ожидание без парковки аренды

Статус: принято (2026-08-11, v0.3)

## Контекст

v0.2 approvals ничего не блокировали. v0.3 нужен first-class approval gate и
семантика «исполнение ждёт человека», но без workflow-движка и без удержания
эксклюзивной аренды часами/днями. Обсуждались: run со статусом `waiting` и
сохранением claim (Option B) и освобождение claim с возобновлением новым
захватом (Option A).

## Решение

**Option A, максимально совместимая с v0.2 Run-семантикой:**

1. Approval получает флаг `gate` (только с `task_id`, CHECK). Пока gate-approval
   `pending`, задача не может быть ни захвачена, ни завершена:
   `409 approval_required`. Проверка — внутри транзакции claim/complete под
   локом строки задачи (та же дисциплина, что readiness): незакоммиченное
   решение невидимо. Частичный индекс `ix_approvals_gate_pending` делает
   probe дешёвым. Любое решение (approve / reject / cancel) открывает gate.
2. `POST /runs/{id}:suspend` — атомарно: run → `suspended` (новый
   **терминальный** статус), claim released, task → todo (удерживается
   gate'ом). Suspend разрешён только живому владельцу (полный claim-gate) —
   зомби получает `stale_claim`.
3. Продолжение — новый claim (новый fencing token) + новый run; рабочее
   состояние переезжает через RunCheckpoints, которые Run Context отдаёт по
   всем прошлым runs задачи. Suspended run остаётся audit-записью.
4. Semantics reject: gate открывается, задача остаётся actionable; никакого
   автоматического бизнес-поведения сервер не выбирает.

## Обоснование

Run остаётся «одной попыткой исполнения, пришпиленной к эпохе claim» — resume
run'а с новым token сломал бы этот инвариант. Освобождение claim на время
ожидания убирает мёртвые аренды и позволяет любому eligible-исполнителю
продолжить после решения (или тому же — обычный случай). Никакого condition
DSL: gate — это ровно «pending gate-approval существует».

## Авторизация gate (уточнено после adversarial review)

Gate — примитив принуждения, поэтому «снять gate» должно требовать той же
власти, что и «решить»: `:cancel` для gate-approval разрешён его автору либо
принципалу, eligible решить (assigned principal / держатель требуемой роли в
scope), иначе `403 not_eligible`. Без этого правила принципал, которого gate
удерживает, снимал бы его сам, имея лишь `approvals.manage` — а эту
permission требует документированный self-suspend flow. Для не-gate approvals
семантика v0.2 (`request`/`cancel` под одной permission) не изменилась.
Gate на терминальной задаче отклоняется (`422 invalid_approval`): он инертен.

## Последствия

- Downgrade v0.3→v0.2 конвертирует suspended runs в failed
  (`failure_reason=suspended_downgraded`) — зафиксировано в миграции.
- Исполнитель обязан checkpoint'ить состояние перед suspend, иначе продолжать
  будет не с чего (протокол это документирует; сервер не заставляет).
- Discovery прячет gated-задачи; диагностика — `GET /tasks/{ref}/claimability`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'CheckConstraint\("NOT gate OR task_id IS NOT NULL", name="gate_requires_task"\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"ix_approvals_gate_pending"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'await check_approval_gate\(session, ctx, task\.id\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/tasks.py, pattern: 'await check_approval_gate\(session, ctx, task\.id\)'}
  repo: control-plane
- route: "POST /runs/{run_id}:suspend"
  repo: control-plane
- grep: {path: migrations/versions/b3d47a1c9e05_harness_protocol_execution_runtime.py, pattern: "suspended_downgraded"}
  repo: control-plane
```
