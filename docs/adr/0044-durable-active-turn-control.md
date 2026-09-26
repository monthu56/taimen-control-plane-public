# ADR-0044: Durable Active Turn Control как Run subresource

Статус: Принято (v0.7, TASK-000003)

## Контекст

Поле `runs.cancel_requested_at` выражало только один кооперативный Stop. Оно не
различало queue, correction, отмену model inference и server-authoritative
force cancel; не имело causal position, ordered acknowledgement и durable
recovery между accepted intent и safe boundary harness.

Harness является недоверенным распределённым клиентом. Process-local queue,
event callback или transcript не могут быть источником истины для управления
Run. В то же время Control Plane не исполняет model/tool loop и не может сам
определить, где безопасно применить steer/redirect.

## Решение

Ввести append-oriented `run_control_messages` с Run-local `seq`, operation
`queue|steer|redirect|request_cancel|force_cancel` и lifecycle
`accepted → applied|rejected|superseded`.

- Create требует `Idempotency-Key` и `expectedRunVersion`.
- Read использует bounded Run-bound opaque cursor `rc1_...`; Run Context несёт
  pending messages для restart recovery.
- Acknowledge выполняет только holder живого Claim с claim id, fencing token,
  expected Run/message versions и safe-boundary marker.
- `request_cancel` запрещает новые actions только после `applied`; уже
  выполнявшийся action может завершиться до boundary.
- `force_cancel` требует `claims.manage`, сразу terminalizes Run, освобождает
  Claim, supersede'ит pending messages и каскадирует applied control message по
  активным `spawned_by` descendants.
- Force-cascade берёт per-tenant transaction advisory lock до row locks:
  overlapping или cyclic descendant closures сериализуются без deadlock.
- State/message/events/outbox commit'ятся атомарно. Event payload содержит
  ссылки и markers, но не directive/reason.
- Legacy `:request-cancel` остаётся wrapper'ом и материализует typed message.

Control Plane не осуществляет process kill. `redirect` и `steer` применяет
harness согласно типизированной операции: model response можно отбросить до
history, завершённый tool result не переписывается.

## Рассмотренные варианты

1. Расширить Run несколькими nullable полями. Отклонено: нет очереди,
   causal ordering, истории нескольких intent и отдельного lifecycle.
2. Хранить команды только как Events. Отклонено: event stream at-least-once и
   предназначен для replay/delivery; удобный authoritative unresolved read
   model всё равно потребовался бы.
3. Оставить команды локальному harness. Отклонено: restart теряет intent,
   другой harness не может безопасно продолжить, нет server authorization.
4. Делать force cancel process kill'ом. Отклонено: Control Plane не владеет
   execution backend; terminal state/fencing и физическое завершение процесса
   являются разными обязанностями.

## Последствия

- Добавляется таблица и новый additive HTTP/SDK/MCP surface.
- Каждый controller обязан работать с optimistic Run version и перечитывать
  очередь после conflict/restart.
- Directive является bounded intentional operational input, а не transcript;
  credential-like content и локальные абсолютные paths отклоняются.
- `force_cancel` может быть дороже обычного message create из-за descendant
  traversal и tenant advisory lock; это сознательная цена редкой privileged
  операции за atomicity и отсутствие deadlock.
- Process supervisor обязан отдельно наблюдать terminal state и завершать
  реальный execution backend; поздние authoritative writes сервер уже
  отклоняет Run/Claim/fencing gates.

## Проверка решения

- HTTP, SDK и MCP create/list/ack;
- idempotency, version conflicts, tenant isolation и stale Claim;
- restart recovery через list/Run Context;
- action-vs-force и competing-message races;
- overlapping cyclic force trees;
- migration upgrade/downgrade/upgrade;
- regression suite существующего Run/cancellation protocol.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- migration: c91f3c7ad8e2
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "run_control_messages"'}
  repo: control-plane
- route: "POST /runs/{run_id}/control-messages/{message_id}:acknowledge"
  repo: control-plane
- grep: {path: src/control_plane/application/queries/execution.py, pattern: 'return "rc1_" \+'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/run_controls.py, pattern: 'async def _cascade_force_cancel_children'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/runs.py, pattern: 'causal_position="legacy:request-cancel"'}
  repo: control-plane
```
