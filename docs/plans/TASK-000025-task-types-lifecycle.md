# TASK-000025: Реестр task_types и настраиваемый lifecycle work item — PLAN

Статус: completed

Ветка: `claude/task-000025-task-types-lifecycle`

База: `main` (`63dc54d`, merge TASK-000026). Alembic head — `f5b91c3e7a24`,
единственный. Новая ревизия ставится поверх него, ветвления не возникает.

## Порядок и его причина

Задача трогает горячий путь claim и completion, поэтому она делается снизу
вверх: сначала домен без БД, затем схема и данные, затем решения ядра, затем
поверхность. Обратный порядок — «сначала эндпоинты, потом перевод claim на
категорию» — оставил бы промежуточные коммиты, в которых часть решений ядра
принимается по ключу, а часть по категории; на существующих данных это
незаметно и вылезает у первого тенанта со своим lifecycle.

## Вертикальные TDD slices

### S1. Домен: категории work item и разбор lifecycle

RED (`tests/unit/test_work_item_domain.py`):

- `parse_lifecycle` с категориями work item принимает шесть статусов
  системного типа и возвращает `Lifecycle` с ожидаемым отображением;
- категория проекта (`planned`, `paused`) в lifecycle work item —
  `invalid_lifecycle_schema`, и наоборот;
- `claimStatus` / `releaseStatus` терминальной категории — отказ;
- `completionStatus` не категории `terminal_success` — отказ;
- `completionStatus` не указан, а статусов `terminal_success` несколько —
  отказ; ровно один — выводится;
- статусов `terminal_success` нет вовсе — отказ;
- внешний `$ref` в `field_schema` — отказ без сетевого обращения;
- документ сверх лимитов размера/глубины/узлов — отказ до движка схем.

GREEN:

- `domain/project.py`: `parse_lifecycle(schema, *, valid_categories)` —
  параметризация допустимого словаря категорий; поведение по умолчанию
  (категории проекта) не меняется;
- `domain/work_item.py`: `WorkItemStatusCategory`, `WORK_ITEM_CATEGORIES`,
  `TERMINAL_CATEGORIES`, `WorkItemLifecycle` (обёртка над `Lifecycle` с тремя
  служебными ключами), `parse_work_item_lifecycle`, `SYSTEM_TASK_TYPE_KEY`,
  `SYSTEM_TASK_LIFECYCLE`, `LEGACY_STATUS_CATEGORIES`.

REFACTOR: `Lifecycle` и guard-функции остаются общими; дублирования разбора
нет.

### S2. Схема и миграция

RED (`tests/integration/test_migration_v08.py`):

- `upgrade → downgrade → upgrade` на непустой базе проходит;
- после upgrade каждая задача имеет `type_id` системного типа тенанта и
  категорию по отображению; **статусы не изменились**;
- задача, созданная до миграции в `blocked`, после апгрейда claimable — как и
  была;
- у каждого тенанта ровно одна версия 1 типа `task`; типы не пересекают
  границу тенанта;
- после downgrade `CHECK` на шесть значений восстановлен, а статус, введённый
  тенантом (`review`), приведён к `todo` по своей категории;
- триггер иммутабельности не даёт изменить `lifecycle_schema` существующей
  версии типа и снять `deprecated`.

GREEN:

- `models.py`: `TaskType`; `Task.type_id`, `Task.system_status_category`,
  новые `CHECK` и индекс `ix_tasks_tenant_category`;
- ревизия `<rev>_work_item_type_and_lifecycle.py`: create table, триггер
  `forbid_task_type_mutation`, seed системного типа на тенант, backfill
  `type_id` и категории, `NOT NULL`, снятие старого `CHECK`, новые `CHECK`,
  индексы. Downgrade — обратный порядок с приведением статусов по категории.

Операционная заметка ревизии: backfill трогает каждую строку `tasks`; индексы
строятся не `CONCURRENTLY` (Alembic держит DDL в транзакции) — окно
обслуживания, как у `1adf50721f1e`.

### S3. Реестр task_types: команды и права

RED (`tests/integration/test_task_types_v08.py`):

- создание версии 1 и 2 одного ключа; версия выдаётся сервером, клиентская
  игнорируется;
- невалидный `lifecycle_schema` — `422`, строка не создана;
- `:deprecate` идемпотентен; `deprecated` не участвует в резолве «последняя
  активная», но задача на него продолжает жить и менять статус;
- попытка изменить существующую версию через SQL — отказ триггера;
- тип чужого тенанта не виден и не резолвится;
- `task_types.manage` требуется для записи, `task_types.read` для чтения;
  ключ с `tasks.write` не может создать тип.

GREEN:

- `domain/enums.py`: `Permission.TASK_TYPES_READ|MANAGE`, `TaskTypeStatus`;
- `application/commands/task_types.py`: `ensure_system_task_type`,
  `get_tenant_task_type`, `resolve_task_type`, `task_type_lifecycle`,
  `create_task_type_version`, `deprecate_task_type` — по образцу
  `project_templates.py`, включая advisory-лок на `(tenant, key)`;
- `commands/bootstrap.py`: системный тип создаётся при bootstrap тенанта.

### S4. Задача: тип при создании, категория в паре

RED (`tests/integration/test_tasks.py`, дополнение):

- задача без `typeKey` получает системный тип и `initialStatus`;
- `typeKey` неизвестного типа — `404`; тип без активных версий — `404`;
- явный статус вне lifecycle — `422 status_not_in_lifecycle`;
- явный статус категории `active`, не равный `initialStatus`, — отказ;
  `backlog` и `initialStatus` — приняты;
- `TaskOut` содержит `typeId`, `typeKey`, `typeVersion`,
  `systemStatusCategory`, а `status` остаётся ключом;
- `task.created` несёт тип и категорию.

GREEN: `commands/tasks.create_task`, `api/v1/task_bodies.py` (тип
резолвится одним запросом на страницу), `schemas.py`.

### S5. Переходы: PATCH, complete, claim, release

RED (`tests/integration/test_task_lifecycle_v08.py`):

- переход по объявленному ребру меняет статус, категорию и версию;
- переход по необъявленному ребру — `422 invalid_transition`, **версия не
  изменилась**, статус не изменился (проверяется перечитыванием после отказа);
- переход в статус вне lifecycle — `422 status_not_in_lifecycle`;
- переход в категорию `terminal_success` через `PATCH` — отказ с указанием на
  `:complete`;
- переход в текущий статус — `422 invalid_transition`;
- `:complete` пишет `completionStatus` типа и категорию `terminal_success`;
- `:complete` при необъявленном ребре — `422`, задача не завершена;
- повторный `:complete` — `409 task_already_completed`; на отменённой задаче —
  `422 task_cancelled` (по категории, не по ключу);
- claim переводит в `claimStatus`, снятие claim — в `releaseStatus`;
- тип, у которого ребро `claimStatus` не объявлено: claim проходит, статус не
  меняется (4.4 SPEC);
- claim задачи в терминальной категории — `task_not_claimable`, независимо от
  ключа (тип с ключом `archived` категории `terminal_cancelled`);
- задача в `blocked` claimable — сохранение сегодняшнего поведения;
- предусловие считается выполненным по `terminal_success`, отменённое
  продолжает блокировать.

GREEN: `commands/claims.py`, `commands/_claim_release.py`, `commands/tasks.py`
(`update_task`, `verify_task_completable`, `finish_locked_task`),
`commands/relations.py`, `queries/discovery.py`, `queries/lists.py`.

### S6. Конкурентность и tenant isolation

RED (`tests/concurrency/test_v08_lifecycle_races.py`,
`tests/integration/test_tenant_isolation.py`):

- два одновременных перехода под одним `If-Match`: ровно один `200`, второй
  `409 version_conflict`; итоговый статус — статус победителя;
- claim против перехода в терминальный статус: наблюдаемых нарушений порядка
  нет, задача не остаётся claimed в терминальной категории;
- тип, статус и категория не пересекают границу тенанта.

### S7. Поверхность и документация

GREEN:

- `control_plane_client`: методы реестра типов и `systemStatusCategory` в
  типах ответа;
- `control_plane_mcp/server.py`: `cp_list_tasks` / `cp_get_task` отдают
  категорию рядом со статусом; новых инструментов управления реестром нет
  (SPEC §12);
- `web/src/lib/api-types.ts` и `task-view.ts`: категория как основа группировки
  вместо жёсткого списка ключей;
- `docs/api.md`: раздел task types, новые поля `TaskOut`, коды `422
  status_not_in_lifecycle` / `invalid_transition`;
- `docs/adr/0048-work-item-type-and-lifecycle.md`;
- `docs/migration-v0.8.md`: порядок применения и downgrade-предупреждение;
- `docs/architecture.md`, `README.md`: статус как пара.

## Gate до completion

1. `make lint`, `make typecheck`, `make test` — зелёные;
2. `upgrade → downgrade → upgrade` на непустой базе — зелёный;
3. verification matrix заполнена фактическими прогонами, а не намерениями;
4. SPEC, PLAN, verification, threat model зарегистрированы как Artifacts;
5. ADR-0015 в репозитории документации отмечает п.1-2 реализованными.

## Известные риски

- **Незаметное расхождение «часть ядра по ключу, часть по категории».**
  Снимается тем, что после S5 в `src/` не остаётся ни одного сравнения
  `Task.status` с литералом; проверяется grep-ом как частью review.
- **Downgrade теряет пользовательские ключи статусов.** Неустранимо;
  документировано в SPEC §9 и в docstring ревизии.
- **Сужение поведения `PATCH` на переходе «в тот же статус».** Единственное
  breaking-изменение; вынесено в SPEC §9 п.4 и в `docs/api.md`.
- **Стоимость резолва типа на горячем пути.** Тип читается один раз на
  операцию под уже взятой блокировкой задачи; для списков — один запрос на
  страницу, как `projectId`.
- **Пересечение с параллельными задачами.** Задача трогает `tasks`,
  `schemas.py`, `router.py`, `docs/api.md`. Alembic-ветвление исключено:
  ревизия ставится на единственный head `f5b91c3e7a24`.
