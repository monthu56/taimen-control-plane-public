# ADR-0004. Lease + fencing token для конкурентной работы

Статус: принято (2026-08-11)

## Контекст

Агенты ненадёжны: зависают, теряют сеть, просыпаются спустя минуты и пытаются
дописать результат. Классическая проблема distributed lock: «мёртвый» держатель
и проснувшийся «зомби».

## Решение

- Claim — аренда с `expires_at`, продлеваемая heartbeat'ом.
- У задачи есть монотонный счётчик `claim_epoch`; каждый новый claim получает
  `fencing_token = ++claim_epoch` (уникальность пары (task, token) закреплена
  constraint'ом).
- Любая рабочая мутация от держателя предъявляет `claimId` + `fencingToken`
  + ожидаемую версию (`If-Match`); токен сверяется с текущим `claim_epoch`.
- Истёкший claim реквизируется атомарно самой командой claim (старый → `stale`,
  новый token выдаётся в той же транзакции) — воркер лишь ускоряет уборку.
- В БД частичный уникальный индекс: не более одного `active` claim на задачу.

## Обоснование

Fencing token — стандартное лекарство от зомби-писателей (Kleppmann, DDIA):
даже если старая сессия «жива» в своём представлении, её токен уже меньше
текущей эпохи, и запись отклоняется на сервере, а не на совести клиента.

## Последствия

- Клиент обязан циклически heartbeat'ить claim и session.
- TTL настраиваемы per-request в пределах серверных границ.
- История claims не удаляется — это часть аудита.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_task_claims_one_active_per_task"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("task_id", "fencing_token", name="uq_task_claims_task_fencing"\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'task\.claim_epoch \+= 1'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/tasks.py, pattern: 'fencing_token != active_claim\.fencing_token or fencing_token != task\.claim_epoch'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'Take over an expired claim: mark it stale and claim the task atomically'}
  repo: control-plane
```
