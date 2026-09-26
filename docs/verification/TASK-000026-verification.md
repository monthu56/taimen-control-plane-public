# TASK-000026: Обобщение external references на work item — Verification

Date: 2026-08-14

Branch: `claude/task-000026-external-references`

Base: `main` (`e0501a6`), Alembic head `a7f2c4d19b60`

Result: PASS

## Delivered contract

- `application/external_entities.py` — реестр entity bindings: тип → загрузчик
  в пределах тенанта + пара permissions. В v0.8 зарегистрированы `project`
  (`projects.read` / `projects.manage`) и `task` (`tasks.read` / `tasks.write`).
- `application/commands/external_references.py` —
  `register_external_reference`, единственная реализация идемпотентности,
  блокировки внешнего ключа и семантики конфликта.
- `application/queries/external_references.py` — прямой поиск по сущности,
  обратный поиск по внешнему ключу, валидация аргументов выборки.
- `POST /api/v1/external-references`, `GET /api/v1/external-references`.
- Проектный скоуп сохранён и переведён на тот же код: команда
  `projects.add_external_reference` и `GET|POST
  /projects/{id}/external-references` стали делегациями.
- SDK: `register_external_reference`, `list_entity_external_references`,
  `lookup_external_reference` (проектные методы сохранены).
- `docs/adr/0047-generic-external-references.md`, обновлён `docs/api.md`.
- Alembic-ревизии **нет**: таблица `external_references` уже была generic.

## Current-session verification evidence

| Gate | Result |
|---|---|
| Новые тесты (`tests/integration/test_external_references_v08.py`) | 16 passed |
| Полный прогон: `uv run pytest` | 666 passed, 13 skipped; 23 Alembic deprecation warnings |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 364 files already formatted |
| `uv run mypy src` | Success; 125 source files |
| `git diff --check` | passed |
| `uv run alembic heads` | single head `a7f2c4d19b60` |
| Migration roundtrip | `upgrade head → downgrade d4e6f8a1b2c3 → upgrade head`; md5 схемы `public` до и после совпадают (`ed7dd9e2…`) — задача действительно не трогает схему |

Тесты выполнялись на изолированном PostgreSQL 16 (порт 5436), поднятом
отдельно от общего `db-test`: в основном рабочем дереве идёт параллельная
работа TASK-000021 (IAM-7), и общая тестовая БД была бы разделяемым
состоянием между двумя run.

## Матрица acceptance

| # | Критерий задачи | Проверка | Result |
|---|---|---|---|
| 1 | Повторная регистрация идемпотентна, дубликата нет | `test_task_reference_registers_and_repeats_idempotently`: повтор → `200`, `version` остаётся 1, второго события нет, в таблице одна строка | PASS |
| 2 | Reference не пересекает границу тенанта | `test_reference_does_not_cross_the_tenant_boundary`: чужой tenant получает `404` на запись и пустую страницу на обратный поиск | PASS |
| 3 | Привязка к чужой или несуществующей сущности отклоняется | `test_reference_to_a_missing_entity_is_rejected` (несуществующий UUID и нерезолвимая ссылка → `404`) | PASS |
| 4 | Право по владельцу сущности, а не по проекту | `test_permission_follows_the_entity_type_not_the_project`: ключ `tasks.*` регистрирует задачу и получает `403` на проект; ключ `projects.*` — наоборот | PASS |
| 5 | Project-эндпоинты без изменения контракта | `test_project_scope_keeps_its_contract` (пути, коды, `entityType`, событие `project.external_reference_added`, форма `404`) + прежний `test_external_reference_identity_is_unique_and_immutable` не менялся и проходит | PASS |
| 6 | Конвенция `SYM-MIG-REF` выражается записями без миграции данных | `test_task_reference_registers_and_repeats_idempotently` и `test_task_can_be_addressed_by_public_id` регистрируют ровно тот ключ, что кодировала конвенция (`system=external-root; type=task; id=SYM-T-070`), в том числе по public id задачи; Alembic-ревизии нет | PASS |
| 7 | Secret scan `metadata` сохранён | `test_secret_material_in_metadata_is_refused` для entity_type=task | PASS |
| 8 | Уникальность в пределах tenant + system + type + external id | `test_external_key_is_exclusive_across_entity_types`: тот же ключ на другую задачу и на проект → `409`, `details` указывают на владельца | PASS |

## Дополнительно проверенные свойства

| Свойство | Проверка | Result |
|---|---|---|
| Неизвестный `entityType` отклоняется до любой записи | `test_unknown_entity_type_is_rejected_before_any_write`: `422 invalid_entity_type`, `details.supported == ["project", "task"]`, таблица пуста | PASS |
| Обратный поиск не является enumeration oracle | `test_reverse_lookup_hides_types_the_caller_cannot_read`: ключ без `tasks.read` получает пустую страницу (не `403`), при этом прямой поиск по типу даёт честный `403` | PASS |
| Полузаданная и смешанная выборка отклоняются | `test_ambiguous_lookup_is_refused`, 5 параметризаций → `422 invalid_external_lookup` | PASS |
| Ссылка на задачу не протекает в выдачу проектов | `test_task_mapping_does_not_leak_into_project_lookup`: `GET /projects?externalSystem=&externalId=` возвращает пусто | PASS |
| Задача адресуется public id | `test_task_can_be_addressed_by_public_id`: запись и прямой поиск по `TASK-NNNNNN` | PASS |

## Известные ограничения

- Удаления external reference через API по-прежнему нет — ограничение ADR-0034
  сохранено осознанно и вне объёма задачи.
- Реестр bindings содержит два типа. Типы work item (WI-1) добавляются в него
  как новые записи; отдельного API для этого не потребуется.
- MCP-инструмент для регистрации ссылок не добавлялся: расширение
  MCP-поверхности — отдельное решение по governance (см. SPEC §11).
