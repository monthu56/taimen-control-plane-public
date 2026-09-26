# ADR-0014. Approval: минимальный примитив губернанса

Статус: принято (2026-08-11, v0.2)

## Контекст

Human-in-the-loop и организационные гейты нужны как примитив, не как policy
engine.

## Решение

- **Одна запись = одно решение.** Multi-approval собирается из нескольких
  записей. Policy language сознательно отсутствует.
- Адресация — ровно один из способов (CHECK `num_nonnulls(...) = 1`):
  - `assigned_principal_id` — конкретный principal;
  - `required_role_id` — любой обладатель роли (scope роли учитывает workspace
    approval'а и его ancestors, как в task eligibility).
- Решение принимается атомарно: `SELECT ... FOR UPDATE` + переход только из
  `pending` — два конкурентных решения дают ровно один терминальный исход,
  второй получает `409 approval_already_decided`.
- Авторизация решения — двухслойная, как у claim'а:
  `approvals.decide` (API permission) ∧ организационная eligibility
  (назначенный principal или требуемая роль). Запрос/отмена — `approvals.manage`.
- Approval **не блокирует задачу автоматически**: связывание «задача ждёт
  approval» — ответственность приложения (при необходимости можно выразить
  dependency-задачей). Оркестрация — вне scope kernel'а.

## Последствия

- Люди и агенты равноправны: агент может запросить approval, человек — решить,
  и наоборот (kind не участвует в правилах).
- Кворумы/эскалации/дедлайны — v2, поверх тех же записей.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'num_nonnulls\(required_role_id, assigned_principal_id\) = 1'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: '"approval_already_decided"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: '\.with_for_update\(\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'code="not_eligible"'}
  repo: control-plane
- route: "POST /approvals/{approval_id}:approve"
  repo: control-plane
```
