# ADR-0037: Операторский redrive parked-события без возможности пропуска

Статус: Принято (v0.5)

## Контекст

v0.4 паркует adapter на отравленном событии и оставляет диагностику в metadata
курсора. Разблокировка требовала ручного UPDATE строки курсора в базе — операция
без аудита, без проверки прав и с возможностью случайно продвинуть курсор мимо
события, создав вечный пробел в памяти.

## Решение

Два операторских endpoint'а под новыми правами:

```
GET  /api/v1/operations/context-adapter                      operations.read
POST /api/v1/operations/context-adapter/{tenantId}:redrive   operations.manage
```

- Диагностика read-only: позиция курсора, отставание (с тем же cap 1000),
  parked-состояние, причина, id отравленного события, число неудач, время
  следующей попытки.
- `:redrive` снимает parked-состояние: обнуляет `failure_count`,
  `next_attempt_at`, `parked_at`, `parked_reason`, `parked_event_id`. Курсор
  **не двигается** — adapter повторит ровно ту же позицию. Пропустить событие
  этим API невозможно; API «продвинуть курсор вперёд» не существует вовсе.
- Операция естественно идемпотентна: повторный redrive на уже разблокированной
  строке — no-op, возвращающий текущее состояние. `Idempotency-Key`
  поддерживается общим механизмом.
- Каждый redrive пишет доменное событие `context_adapter.redriven`
  (`entity_type = "event_consumer"`) в той же транзакции — аудит обязателен.
- `tenantId` в пути должен совпадать с Tenant вызывающего; чужой Tenant — `404`
  (защита от enumeration).

Crash-неоднозначность («упали между confirm провайдера и commit курсора») не
меняется: повтор доставки семантически дедуплицируется Memory по стабильной
идентичности `event:<uuid>` (ADR-0026). Redrive после такого падения безопасен по
той же причине.

CLI: `control-plane ops adapter status` и `control-plane ops adapter redrive`.

## Последствия

- Оператор больше не ходит в базу руками, а действие остаётся в журнале.
- Единственный способ разобраться с отравленным событием — починить причину
  (mapping, провайдер, данные) и повторить. Это осознанно жёстко: тихий пропуск
  ломает воспроизводимость памяти.
- Если событие отравлено неисправимо, остаётся операторская процедура
  «пересобрать состояние» (ADR-0038), а не пропуск.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/api/v1/operations.py, pattern: 'prefix="/operations"'}
  repo: control-plane
- route: "GET /context-adapter"
  repo: control-plane
- route: "POST /context-adapter/{tenant_id}:redrive"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/operations.py, pattern: 'event_type="context_adapter\.redriven"'}
  repo: control-plane
- absent: {path: src/control_plane/api/v1/operations.py, pattern: ':(advance|skip)"'}
  repo: control-plane
- grep: {path: src/control_plane_cli/main.py, pattern: 'add_parser\("redrive"'}
  repo: control-plane
```
