# ADR-0034: Generic external references как mapping, а не источник истины

Статус: Принято (v0.5)

## Контекст

Миграция из внешних систем и интеграции требуют находить внутреннюю сущность по
её идентификатору во внешней системе (тикет, карточка, репозиторий). Соблазн —
положить внешний id в колонку сущности; тогда core узнаёт про конкретный продукт
и появляется постоянный dual-write.

## Решение

Отдельная product-neutral таблица `project_external_references`:
`tenant_id`, `entity_type`, `entity_id`, `external_system`, `external_type`,
`external_id`, `metadata`, `created_by`, timestamps.

- `UNIQUE (tenant_id, external_system, external_type, external_id)` — один
  внешний ключ отображается ровно в одну внутреннюю сущность внутри Tenant.
- Индекс `(tenant_id, entity_type, entity_id)` для обратного поиска.
- Поля идентичности неизменяемы: триггер БД запрещает UPDATE `entity_type`,
  `entity_id`, `external_system`, `external_type`, `external_id`. Меняется только
  `metadata` (и `version`/`updated_at`), с событием
  `project.external_reference_updated`.
- `POST /projects/{id}/external-references` идемпотентен по внешнему ключу: если
  ключ уже указывает на эту же сущность — обновляется `metadata` и возвращается
  `200`; если на другую — `409 external_reference_conflict`.
- Поиск: `GET /projects?externalSystem=&externalType=&externalId=` возвращает
  проект по внешней ссылке, всегда в пределах Tenant вызывающего.
- `metadata` не содержит секретов: при записи применяется тот же guard, что и к
  Project config (ADR-0035 в части `secretRef`).

External reference — только отображение. Control Plane никогда не пишет во
внешнюю систему по этой записи и не считает её состояние авторитетным.

## Последствия

- Нет product-specific колонок ни в одной доменной таблице.
- Нет постоянного dual-write: направление всегда «внешний id → внутренний id»,
  разрешение — задача импортёра, а не core.
- Изменить внешний идентификатор нельзя — нужно создать новую запись и удалить
  старую. В v0.5 удаления через API нет; операция признана вне scope и отмечена в
  известных ограничениях.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'name="uq_external_references_external_key"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'Index\("ix_external_references_entity", "tenant_id", "entity_type", "entity_id"\)'}
  repo: control-plane
- grep: {path: "migrations/versions/*.py", pattern: 'CREATE TRIGGER external_references_identity_immutable'}
  repo: control-plane
- route: "POST /projects/{project_id}/external-references"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/external_references.py, pattern: '"external_reference_conflict"'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/projects.py, pattern: 'alias="externalSystem"'}
  repo: control-plane
```
