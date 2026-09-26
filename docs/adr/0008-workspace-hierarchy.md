# ADR-0008. Иерархия workspaces: adjacency list + advisory lock

Статус: принято (2026-08-11, v0.2)

## Контекст

Workspace задаёт организационный scope (отделы, команды, проекты) и участвует
в scope-семантике ролей. Нужны: иерархия, уникальность slug среди siblings,
запрет циклов, move поддерева. Варианты хранения: adjacency list, materialized
path, nested sets, ltree.

## Решение

- **Adjacency list** (`parent_id`), `parent_id IS NULL` = корневой уровень
  (корней может быть несколько).
- Уникальность slug среди siblings — два частичных уникальных индекса
  (отдельный для корневого уровня, т.к. NULL в обычном UNIQUE не сравнивается).
- Обходы (ancestors, subtree) — recursive CTE по запросу; глубина иерархий
  организаций мала, денормализация не нужна.
- **Структурные мутации** (create, move, смена slug, archive) сериализуются
  per-tenant advisory lock'ом `pg_advisory_xact_lock(hash('cp:ws:<tenant>'))`:
  проверка цикла (CTE по поддереву) и проверка slug выполняются без гонок; два
  конкурентных move не могут собрать цикл совместно. Индексы остаются backstop.
- Archive вместо delete; архивирование требует отсутствия активных детей;
  архивный workspace не принимает новые под-workspaces и задачи.

## Обоснование

Adjacency list — простейшая модель, полностью покрывающая MVP-операции;
materialized path/nested sets ускоряют массовые subtree-запросы ценой сложных
инвариантов при move — у нас таких запросов нет. Advisory lock на дерево
тенанта дешевле и проще, чем блокировка путей: структурные изменения дерева
редки, конкуренция за лок незначима.

## Последствия

- Ancestor-запросы стоят один CTE (мал при реальных глубинах).
- Все структурные мутации одного тенанта сериализованы — приемлемо.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_workspaces_root_slug"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_workspaces_sibling_slug"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: 'pg_advisory_xact_lock\(func\.hashtextextended\(f"cp:ws:\{tenant_id\}"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_cycle"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_has_active_children"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_archived"'}
  repo: control-plane
```
