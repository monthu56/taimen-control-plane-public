# ADR-0031: Project Profile поверх Workspace и настраиваемый lifecycle

Статус: Принято (v0.5)

## Контекст

Платформа должна поддерживать портфели, проекты, подпроекты и рабочие потоки с
единым наследованием прав. Два независимых дерева (Workspaces и Projects) могли
бы разойтись, и тогда «где эта задача» и «кто на неё имеет права» отвечались бы
по-разному.

Одновременно проектам нужны пользовательские статусы («discovery», «pilot»,
«production»), но core не может принимать решения по произвольной строке.

## Решение

### Профиль, а не отдельное дерево

Workspace остаётся единственным авторитетным деревом. Project — это
`project_profiles`, привязанный к Workspace отношением один-к-одному
(`UNIQUE (workspace_id)` на уровне БД). Поля `parent_project_id` нет: родитель
проекта вычисляется как ближайший предок Workspace, у которого есть профиль.
Project API — проекция Workspace + Project Profile.

Инварианты, обеспеченные БД и командой:

- `UNIQUE (workspace_id)` — конкурентное создание двух профилей даёт ровно один
  успех, остальные получают `409 project_exists`;
- составной FK `(tenant_id, workspace_id)` → `workspaces(tenant_id, id)` — профиль
  физически не может ссылаться на чужой Workspace;
- archived Workspace не может получить новый профиль (`422 workspace_archived`);
- `project.archive` не трогает Workspace, задачи, события и историю конфигурации;
- `workspace.archive` не архивирует профиль неявно: у Workspace с активным
  профилем архивация отклоняется с `422 workspace_has_active_project` — оператор
  сначала архивирует Project. Обратный порядок разрешён: архивированный Project
  на активном Workspace допустим (проект закончен, контейнер живёт).

Системные поля (`status_key`, `system_status_category`, `owner_principal_id`,
`start_date`, `target_date`, `active_config_revision_id`, `version`) — колонки, а
не JSON. В JSON живут только расширяемые `custom_fields` (валидируются
`field_schema` точной версии шаблона) и `settings` (слой effective config,
см. ADR-0032).

### Lifecycle: пользовательский ключ, системная категория

`lifecycle_schema` шаблона имеет фиксированную форму:

```json
{
  "initialStatus": "discovery",
  "statuses": [{"key": "discovery", "displayName": "Discovery", "category": "planned"}],
  "transitions": [{"from": "discovery", "to": ["pilot", "cancelled"]}]
}
```

Системных категорий ровно пять: `planned`, `active`, `paused`,
`terminal_success`, `terminal_cancelled`. Валидация схемы требует уникальности
ключей, существования `initialStatus`, корректной категории у каждого статуса и
ссылок переходов только на известные ключи.

Решения core принимаются исключительно по `system_status_category` — она
хранится денормализованной колонкой на профиле и пересчитывается при каждой смене
статуса и при смене версии шаблона. API, события и проекции сохраняют
пользовательский `statusKey`.

Переход выполняется командой `POST /projects/{id}:transition` с обязательным
`If-Match`, под row lock профиля, и порождает отдельное событие
`project.status_changed`. Переход, не объявленный в `transitions`, отклоняется
(`422 invalid_transition` с перечнем допустимых). Это не workflow engine: нет
условий, действий, таймеров и ветвлений — только объявленный граф.

### Активация ревизии не может «сломать» состояние

Активация config revision и смена версии шаблона перевалидируют текущий
`status_key` по новому lifecycle. Если статус в новой схеме отсутствует,
операция отклоняется до commit с `422 status_not_in_lifecycle`; мигрировать
статус нужно явно.

## Последствия

- Подпроект — это дочерний Workspace с собственным профилем; никакой отдельной
  сущности не появляется.
- «Проект» и «контейнер» разделены: у Workspace может не быть профиля, и это
  нормальное состояние (workstream, команда, папка).
- Execution Workspace (checkout, worktree, sandbox) не участвует в этом дереве и
  в v0.5 не моделируется вовсе.
- Денормализованная `system_status_category` требует пересчёта в двух местах
  (transition и смена шаблона) — цена за то, что discovery и фильтры не тянут
  lifecycle-схему в каждый запрос.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("workspace_id", name="uq_project_profiles_workspace"\)'}
  repo: control-plane
- route: "POST /projects/{project_id}:transition"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/projects.py, pattern: 'event_type="project\.status_changed"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_has_active_project"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/projects.py, pattern: '"status_not_in_lifecycle"'}
  repo: control-plane
```
