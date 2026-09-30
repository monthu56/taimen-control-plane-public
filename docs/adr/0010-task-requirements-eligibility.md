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

## Амендмент 2026-09-30: участники workspace и держатели его ролей (TASK-001194)

Найдено на приёмке R024: у workspace «Оплата счетов (демо)» `GET
/workspaces/{id}/members` пуст, а обе его роли назначены владельцу — он в
`GET /roles/{id}/principals?workspaceId=` и берёт задачи по роли. Консоль и
ассистент читали `members` и говорили «никто не назначен на роли».

Решение:

1. **Членство остаётся явным.** `WorkspaceMember` — организационная запись,
   которую ставит и снимает `POST /workspaces/{id}/members[...:remove]`;
   назначение роли его не создаёт и отзыв роли не снимает. Так членство не
   исчезает вместе с ролью и не появляется у всех держателей роли уровня
   tenant. Eligibility по-прежнему считает только роли (см. «Решение»).
   `GET /workspaces/{id}/members` — только явное членство, форма не меняется.
2. **Участники — отдельный список**: `GET /workspaces/{id}/participants`.
   Principal — участник, если он явный член workspace или держит роль
   workspace: у него есть назначение, которое действует в workspace по
   правилу CP-ADR-0068 п.7 (уровень tenant, этот workspace или его предок —
   `role_assignment_scope`), и либо роль принадлежит этому workspace, либо
   назначение сделано на этот workspace. Назначение роли уровня tenant на
   уровне tenant участником не делает — иначе участник был бы у каждого
   workspace. Для каждой роли, созданной в workspace, держатели из
   `GET /roles/{id}/principals?workspaceId=<этот>` совпадают с участниками,
   у которых эта роль в `roles`.
3. **Элемент** — `{principalId, kind, displayName, status, member, roles}`.
   `member` — есть ли явное членство; `member: false` при непустом `roles` —
   «держит роль, но не участник по членству». `roles[]` — `{roleId, slug,
   name, roleWorkspaceId, assignmentWorkspaceId}`: чья роль и на каком
   уровне назначена. Страница и курсор — как у `GET /roles/{id}/principals`
   (по principal'ам); статус principal'а не фильтруется.
4. **Право** — `workspaces.read` и (`org.read` или `principals.read`): кто
   держит какую роль — данные организации, как у держателей роли. Чужой и
   несуществующий workspace — одинаковый `404`.

Следствие: консоль («Люди и роли») и ассистент берут людей workspace из
`participants`, а не из `members`; `members` нужен там, где правят явное
членство. Клиент — `list_workspace_participants`.

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
- grep: {path: src/control_plane/api/v1/workspaces.py, pattern: '"/workspaces/\{workspace_id\}/participants"'}
  repo: control-plane
```
