# ADR-0009. Семантика Role vs Capability vs Skill

Статус: принято (2026-08-11, v0.2)

## Контекст

Нужно описывать исполнителей (людей и агентов одинаково) так, чтобы задача
могла требовать «какого» исполнителя, не называя конкретного. Три разных
вопроса нельзя смешивать в одну сущность.

## Решение

| Примитив | Вопрос | Пример | Хранение |
|---|---|---|---|
| **Role** | Кто ты в организации? | `software-engineer`, `legal-lead` | `roles` + `principal_roles` (scope: tenant-wide или workspace-поддерево) |
| **Capability** | Что ты способен делать? | `code.python`, `github.write` | `capabilities` + `principal_capabilities` (metadata JSONB: level и т.п.) |
| **Skill** | Какой исполняемый интерфейс доступен? | `github.create_pr` | `skills` (registry: protocol, config, schemas, версии) + `principal_skills` |

Ключевые границы:

- **Роль ≠ permission.** API-авторизация остаётся исключительно за
  permissions API-ключей. Роль participates только в *task eligibility* и
  *approval eligibility*. `software-engineer` без `tasks.claim` в ключе не
  может claim'ить ничего.
- Scope роли: assignment c `workspace_id = NULL` действует на весь tenant;
  assignment на workspace W действует в W и всём его **поддереве**.
- Skill registry — только метаданные (protocol: mcp/http/local/opencode/custom,
  config, input/output schemas). Control Plane не исполняет skills. Версии —
  отдельные записи `(tenant, name, version)`; optimistic concurrency —
  отдельная колонка `row_version` (ETag `"skill-<row_version>"`).
- Capability inference (вывод способностей) сознательно не реализуется.

## Последствия

- Профиль principal = роли + capabilities + skills; един для людей и агентов.
- Замена auth-механизма в будущем не затрагивает организационную модель.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "principal_roles"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "principal_capabilities"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "principal_skills"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("tenant_id", "name", "version", name="uq_skills_tenant_name_version"\)'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/org.py, pattern: 'format_etag\("skill", skill\.row_version\)'}
  repo: control-plane
```
