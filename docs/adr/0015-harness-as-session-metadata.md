# ADR-0015. Harness — метаданные Session, а не отдельный aggregate

Статус: принято (2026-08-11, v0.3)

## Контекст

v0.3 вводит понятие harness (Claude Code, CLI, agent daemon...) — исполняющей
среды principal'а. Нужны observability (кто/чем подключён) и protocol
negotiation. Кандидаты: отдельная сущность `HarnessInstance` или расширение
существующей `Session`.

## Решение

Harness — колонки живой сессии: `harness_type`, `harness_version`,
`protocol_version`, `harness_capabilities` (JSONB), `hostname`, `environment`
(JSONB). Все nullable — до-v0.3 клиенты открывают сессии как раньше.
Отдельной сущности нет.

Continuity после рестарта обеспечивают уже существующие Principal + Claim +
Run + Events (+ v0.3 Checkpoints): новая инкарнация harness открывает новую
сессию и восстанавливает состояние через `GET /harness/context`; чужие
активные claims она не наследует автоматически.

## Обоснование

У harness нет независимого lifecycle: он «живёт», ровно пока живо его
подключение — а это и есть session-lease. Отдельный aggregate дал бы вторую
сущность с теми же heartbeat/expiry-механиками и вопросом консистентности
между ними. Регистрация при open-session атомарна и попадает в событие
`session.opened` (payload: harnessType, protocolVersion).

## Последствия

- Историю подключений конкретного «инстанса» harness между сессиями сервер не
  связывает (при необходимости клиент кладёт свой instance-id в metadata).
- `ix_sessions_principal_active` ускоряет bootstrap-контекст.
- Появись у harness настоящий независимый lifecycle (например, парк
  зарегистрированных раннеров) — сущность можно выделить позже, не ломая
  протокол: блок `harness` в open-session останется тем же контрактом.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'harness_type: Mapped\[str \| None\]'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'protocol_version: Mapped\[str \| None\]'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'harness_capabilities: Mapped\[list\[str\] \| None\] = mapped_column\(JSONB'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"ix_sessions_principal_active"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/sessions.py, pattern: '"harnessType"'}
  repo: control-plane
- absent: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "harness_instances"'}
  repo: control-plane
```
