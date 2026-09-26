# ADR-0010. Task requirements и eligibility при claim

Статус: принято (2026-08-11, v0.2)

## Контекст

Задача должна описывать нужного исполнителя декларативно. Нужен детерминированный
и проверяемый в транзакции claim'а предикат, без expression language.

## Решение

- Хранение — **нормализованная таблица** `task_requirements` (kind:
  role|capability|skill + FK на соответствующий реестр; CHECK «ровно одна
  ссылка», частичные уникальные индексы против дублей). Никакого сырого JSONB.
- API принимает slugs/имена (`{"roles": ["software-engineer"], ...}`),
  сервер резолвит их в id при создании/замене набора; неизвестное имя —
  `422 unknown_requirement`. Роли резолвятся от workspace задачи вверх по
  ancestors (ближайший scope выигрывает), затем tenant-global.
- Семантика: **все перечисленные требования обязательны** (AND). ANY/OR и
  expression language — сознательно вне scope.
- Eligibility проверяется в транзакции claim'а, под локом строки задачи:
  - роль: есть assignment с scope NULL или из множества {workspace задачи +
    ancestors} — т.е. роль, выданная на предка, покрывает поддерево;
  - capability: точное наличие assignment;
  - skill: наличие у principal skill **с тем же именем** (любая версия) —
    версия требования фиксируется для аудита, но матчинг по имени.
- Отказ — `403 not_eligible` со списками missing{Roles,Capabilities,Skills}.
- Claim разрешён только при одновременном выполнении: API permission
  (`tasks.claim`) ∧ eligibility ∧ readiness (ADR-0011) ∧ concurrency-правила.
- Workspace membership в v0.2 — организационные метаданные, НЕ участвуют в
  eligibility (документированное упрощение).

## Последствия

- Замена набора требований — атомарная (PATCH задачи под If-Match + claim gate).
- Матчинг skill по имени означает: обновление версии skill в реестре не
  выбивает исполнителей со старой версией.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'name="exactly_one_ref"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/eligibility.py, pattern: '"unknown_requirement"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/eligibility.py, pattern: 'code="not_eligible"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/eligibility.py, pattern: 'if missing_roles or missing_capabilities or missing_skills'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'await check_claim_eligibility\(session, ctx, task'}
  repo: control-plane
```
