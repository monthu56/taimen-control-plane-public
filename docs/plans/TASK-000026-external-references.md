# TASK-000026: Обобщение external references на work item — PLAN

Статус: completed

Ветка: `claude/task-000026-external-references`

База: `main` (`e0501a6`, merge TASK-000011). Alembic head — `a7f2c4d19b60`.

## Ограничение параллельной работы

В рабочем дереве `control-plane` на момент старта лежит незакоммиченная работа
TASK-000021 (IAM-7), чей run активен в другой сессии, включая **новую
незакоммиченную ревизию** `f5b91c3e7a24` поверх `a7f2c4d19b60`. Поэтому:

- работа ведётся в отдельном git worktree от чистого `main`, чтобы не
  захватить чужие изменения в коммиты;
- эта задача **не добавляет Alembic-ревизию**: таблица `external_references`
  уже generic, схема не меняется. Multiple heads с IAM-7 не возникает —
  повторение урока TASK-000008 исключено по построению.

## Вертикальные TDD slices

### S1. Реестр entity bindings

RED:

- unit: реестр отдаёт binding для `project` и `task` и поднимает
  `invalid_entity_type` на неизвестный тип, перечисляя поддерживаемые;
- unit: набор типов реестра совпадает с тем, что перечислен в `details.supported`
  (перечисление не расходится с реализацией).

GREEN:

- `application/external_entities.py`: `EntityBinding` (тип, загрузчик,
  read/manage permission), `ENTITY_BINDINGS`, `resolve_entity_binding`;
- загрузчики `project` (UUID, tenant-scoped) и `task` (UUID или public id,
  tenant-scoped), оба возвращают сущность или `NotFoundError`.

REFACTOR: `entity_type="project"` перестаёт быть литералом в командах — только
через реестр.

### S2. Обобщённая команда регистрации

RED:

- integration: регистрация ссылки на задачу создаёт строку с
  `entity_type="task"` и событием `task.external_reference_added`;
- integration: повтор того же тела — `200`, без новой строки, без роста
  `version`, без второго события;
- integration: тот же ключ с другим `metadata` — `200`, `version += 1`,
  событие `task.external_reference_updated`;
- integration: тот же ключ на другую сущность (и на другой тип с тем же UUID)
  — `409 external_reference_conflict`;
- integration: задача чужого тенанта — `404`, ссылка не создаётся;
- integration: `metadata` с secret material — отказ; превышение границ
  размера/глубины — отказ;
- integration: проектный путь через новую команду даёт **прежние** событие
  `project.external_reference_added` и коды `201`/`200`/`409`.

GREEN:

- `application/commands/external_references.py`:
  `register_external_reference(session, ctx, *, entity_type, entity_ref, …)`;
- `commands/projects.add_external_reference` становится делегацией (сигнатура
  сохраняется — её вызывает существующий API).

### S3. Обобщённые HTTP-эндпоинты

RED:

- integration: `POST /external-references` с `entityType=task` требует
  `tasks.write`; ключ только с `projects.manage` получает `403`;
- integration: `POST` с `entityType=project` требует `projects.manage`;
- integration: неизвестный `entityType` — `422 invalid_entity_type` до любой
  записи;
- integration: `GET /external-references?entityType=&entityId=` отдаёт
  страницы, `nextCursor` завершается, чужой тенант не виден;
- integration: обратный поиск `?externalSystem=&externalId=` находит запись;
  без права чтения найденного типа — **пустая страница**, не `403`;
- integration: смешение прямого и обратного режимов и полузаданный обратный
  поиск — `422 invalid_external_lookup`;
- integration: `Idempotency-Key` на `POST` ведёт себя как на прочих write.

GREEN:

- `api/v1/external_references.py` + регистрация в `router.py`;
- schemas: `ExternalReferenceRegisterRequest` (с `entityType`/`entityId`),
  переиспользование `ExternalReferenceOut`.

### S4. Совместимость проектного скоупа

RED:

- integration: `GET|POST /projects/{id}/external-references` — прежние коды,
  тело, ETag-поведение и события (тест на неизменность контракта);
- integration: `GET /projects?externalSystem=&externalType=&externalId=`
  продолжает находить проект; ссылка на задачу с тем же `externalId` не
  превращает задачу в проект и не попадает в выдачу проектов.

GREEN: проектные обработчики делегируют в обобщённые; отдельной логики нет.

### S5. Клиент, MCP-нейтральность и документация

RED:

- client-тест: `register_external_reference` и `list_external_references`
  в SDK ходят по обобщённым путям; проектные методы SDK сохраняются.

GREEN:

- `control_plane_client/client.py`: обобщённые методы рядом с проектными;
- `docs/api.md`: раздел external references, коды ошибок,
  `invalid_entity_type` в таблице `422`;
- `docs/adr/0047-generic-external-references.md`: обобщение ADR-0034 на work
  item — реестр bindings, право по типу сущности, единый `404`/пустая страница
  в обратном поиске;
- `docs/verification/TASK-000026-verification.md` и
  `TASK-000026-threat-model.md`.

### S6. Закрытие долга ADR-0014

GREEN (в репозитории документации):

- ADR-0014: конвенция `SYM-MIG-REF` помечается закрытой; дедупликация импорта
  и обратное отображение выражаются записями external reference;
- ADR-0015: п.5 отмечается реализованным в TASK-000026.

## Gate до completion

1. `make lint`, `make typecheck`, `make test` — зелёные;
2. миграции upgrade → downgrade → upgrade без изменений схемы (проверка того,
   что задача действительно не трогает схему);
3. verification matrix заполнена фактическими прогонами;
4. SPEC, PLAN, verification, threat model зарегистрированы как Artifacts.

## Известные риски

- **Расхождение проектного и обобщённого путей.** Снимается тем, что
  проектный путь физически вызывает обобщённую команду; отдельная ветка логики
  для проектов не оставляется.
- **Обратный поиск как enumeration oracle.** Снимается пустой страницей вместо
  `403` и отсутствием различий между «нет ключа» и «нет права на тип».
- **Свободный `entity_type` от клиента.** Снимается реестром: неизвестный тип
  отклоняется до любой записи, поэтому занять чужой внешний ключ на
  несуществующий тип нельзя.
- **Конфликт с IAM-7 при merge.** Пересечение ограничено `api/v1/router.py`,
  `docs/api.md` и `schemas.py`; Alembic-ветвление исключено.
