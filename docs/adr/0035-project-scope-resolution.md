# ADR-0035: Принадлежность задачи проекту выводится из дерева Workspace

Статус: Принято (v0.5)

## Контекст

Задачам нужен проектный scope: discovery, списки и контекст должны уметь
фильтроваться по проекту. Самое очевидное решение — колонка `tasks.project_id` —
создаёт второе поле принадлежности, которое неминуемо разойдётся с
`workspace_id` при перемещении Workspace.

## Решение

Второго поля нет. Проект задачи вычисляется сервером из дерева Workspace:

- владеющий проект = ближайший предок Workspace (включая сам Workspace задачи),
  у которого есть Project Profile;
- задача прямо в Project Workspace принадлежит этому проекту;
- обычный Workspace внутри проекта наследует проект;
- вложенный Workspace с собственным профилем начинает новый scope.

Две явные семантики scope, обе выражены рекурсивными CTE в PostgreSQL:

- **exact** — Project Workspace и его обычные потомки; обход останавливается
  (не включая) на любом Workspace с собственным профилем;
- **`includeSubprojects=true`** — полное поддерево Project Workspace.

`projectId` — фильтр запроса, а не колонка: он разворачивается в множество
`workspace_id` и попадает в `WHERE` **до** пагинации, поэтому страницы не
пропускают и не дублируют записи. Существующие фильтры `workspaceId` /
`includeDescendants` работают как раньше; комбинация `projectId` +
`workspaceId` пересекает оба множества.

`TaskOut` получает вычисляемое поле `projectId`. Для списков оно резолвится одним
batch-запросом (recursive CTE, засеянный множеством `workspace_id` страницы),
а не N запросами.

Discovery дополнительно исключает задачи, чей владеющий проект архивирован:
архивный проект не выдаёт новую доступную работу. Множество «workspace'ов
архивных проектов» строится одним CTE и только когда в Tenant есть хотя бы один
архивный проект. Уже активные Claim и Run при этом не ломаются: смена статуса
проекта не влияет ни на heartbeat, ни на завершение начатой работы — авторитетным
гейтом остаётся claim (ADR-0004).

## Последствия

- Перемещение Workspace автоматически меняет владеющий проект всех задач в нём —
  и это правильное поведение: организационная и проектная иерархии совпадают по
  определению.
- Резолюция проекта стоит одного рекурсивного CTE на запрос. На дереве в 10 000
  Workspace это измерено и признано приемлемым (бенчмарк v0.5); отдельных
  денормализованных колонок не вводится, пока измерение не покажет обратного.
- «Задача без Workspace» (`workspace_id IS NULL`) не принадлежит ни одному
  проекту и не попадает ни в один `projectId`-фильтр.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/api/v1/tasks.py, pattern: 'alias="projectId"'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/tasks.py, pattern: 'alias="includeSubprojects"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/projects.py, pattern: 'WITH RECURSIVE'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/task_bodies.py, pattern: 'projectId='}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/discovery.py, pattern: 'archived_project_workspace_ids\('}
  repo: control-plane
```
