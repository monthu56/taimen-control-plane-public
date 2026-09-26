# TASK-000025: Реестр task_types и lifecycle work item — verification

Статус: completed

Ветка: `claude/task-000025-task-types-lifecycle`; база `main` (`63dc54d`).

Связанные документы: `docs/specs/TASK-000025-task-types-lifecycle.md`,
`docs/plans/TASK-000025-task-types-lifecycle.md`,
`docs/verification/TASK-000025-threat-model.md`,
`docs/adr/0048-work-item-type-and-lifecycle.md`,
`docs/migration-v0.8.md`.

## Прогоны

| Проверка | Команда | Результат |
|---|---|---|
| Линт и формат | `uv run ruff check . && uv run ruff format --check .` | зелёный, 385 файлов |
| Типы | `uv run mypy src` | `Success: no issues found in 130 source files` |
| Полный набор | `uv run pytest` | **785 passed, 13 skipped** (было 742 до задачи) |
| Новое в задаче | пять файлов ниже | **84 passed** |

| Файл | Тестов |
|---|---|
| `tests/unit/test_work_item_domain.py` | 41 |
| `tests/integration/test_task_types_v08.py` | 14 |
| `tests/integration/test_task_lifecycle_v08.py` | 22 |
| `tests/integration/test_migration_v08.py` | 4 |
| `tests/concurrency/test_v08_lifecycle_races.py` | 3 |

## Acceptance задачи

| # | Критерий | Чем закрыт | Статус |
|---|---|---|---|
| 1 | задача не принимает статус, не объявленный в lifecycle своего типа | `test_status_outside_the_lifecycle_is_rejected`, `test_creation_status_must_be_initial_or_backlog` | ✅ |
| 2 | переход по необъявленному ребру отклоняется и **не меняет версию** | `test_undeclared_transition_changes_neither_status_nor_version`, `test_refused_transitions_never_move_the_version` (8 конкурентных отказов, версия та же) | ✅ |
| 3 | claimability и завершение считаются по категории, а не по ключу | `test_core_decisions_follow_the_category_not_the_key` (статус с ключом `done` категории `active` — claimable), `test_claiming_a_terminal_task_is_refused_whatever_the_key`, `test_completing_a_terminal_task_reports_by_category` | ✅ |
| 4 | `lifecycle_schema` валидируется локально, внешние `$ref` запрещены | `test_remote_ref_in_field_schema_is_refused`, `test_pathological_nesting_is_refused`, `test_too_many_statuses_are_refused` | ✅ |
| 5 | migration `upgrade → downgrade → upgrade` проходит | `test_existing_tasks_keep_their_status_and_gain_a_category`, `test_a_blocked_task_survives_the_roundtrip_claimable` | ✅ |
| 6 | существующие задачи сохраняют статусы и остаются claimable | те же два теста; статусы `backlog/todo/blocked` не изменились, задача в `blocked` claimable после roundtrip | ✅ |
| 7 | `status` остаётся в API ключом, категория добавлена рядом | `test_default_type_and_status_reproduce_pre_v08_behaviour`; **все восемь существующих `tests/integration/test_tasks.py` прошли без правок** | ✅ |
| 8 | tenant isolation и конкурентные переходы закреплены тестами | `test_task_type_does_not_cross_the_tenant_boundary`, `test_concurrent_transitions_produce_one_winner` (1×200 / 4×409), `test_claim_races_completion` | ✅ |
| 9 | полный gate | см. таблицу прогонов | ✅ |

## Threat model

| # | Угроза | Тест | Статус |
|---|---|---|---|
| T1 | статус вне lifecycle | `test_status_outside_the_lifecycle_is_rejected` | ✅ |
| T2 | обход процесса необъявленным ребром | `test_undeclared_transition_changes_neither_status_nor_version` | ✅ |
| T3 | ложное завершение через `PATCH` | `test_patch_cannot_reach_terminal_success` | ✅ |
| T4 | решение ядра по имени статуса | `test_core_decisions_follow_the_category_not_the_key`; плюс grep: в `src/` не осталось сравнений `Task.status` с литералом (единственное вхождение — фильтр `?status=` в `queries/lists.py:200`) | ✅ |
| T5 | кросс-тенантное применение типа | `test_task_type_does_not_cross_the_tenant_boundary` | ✅ |
| T6 | правка lifecycle под живыми задачами | `test_version_content_is_immutable_even_against_raw_sql`, `test_deprecate_is_idempotent_and_one_way` | ✅ |
| T7 | тип без успешного финала | `test_lifecycle_without_success_is_refused`, параметр `no-success-status` в `test_invalid_lifecycle_is_refused_and_writes_nothing` | ✅ |
| T8 | терминальный `claimStatus` | `test_service_status_must_not_be_terminal`, параметр `terminal-claim-status` | ✅ |
| T9 | неоднозначный финал | `test_ambiguous_completion_status_is_refused` | ✅ |
| T10 | SSRF через `$ref` | `test_remote_ref_in_field_schema_is_refused` | ✅ |
| T11 | патологическая схема | `test_pathological_nesting_is_refused`, `test_too_many_statuses_are_refused` | ✅ |
| T12 | потерянное обновление | `test_concurrent_transitions_produce_one_winner` | ✅ |
| T13 | claim против завершения | `test_claim_races_completion` | ✅ |
| T14 | deprecate обездвиживает задачи | `test_deprecated_version_stops_resolving_but_keeps_its_tasks_alive` | ✅ |
| T15 | эскалация «задачи → процесс» | `test_task_types_require_their_own_permission` | ✅ |
| T16 | клиент задаёт категорию | `test_client_cannot_set_the_category` (`400`, поля нет в схеме запроса) | ✅ |

Дополнительно закрыт инвариант, которого не было в модели угроз: последняя
активная версия системного типа не депрецируется
(`test_the_last_active_system_type_version_cannot_be_deprecated`) — иначе
`POST /tasks` без `typeKey` стал бы `404` для всего тенанта.

## Сохранённое поведение (регресс-контроль)

| Поведение до v0.8 | Проверка |
|---|---|
| задача в `blocked` claimable | `test_blocked_category_remains_claimable`, `test_a_blocked_task_survives_the_roundtrip_claimable` |
| отменённое предусловие продолжает блокировать | `test_cancelled_prerequisite_keeps_blocking` |
| `cancelled` достижим через `PATCH` | `test_terminal_cancelled_stays_reachable_by_patch` |
| claim переводит задачу в рабочий статус, release возвращает | `test_claim_and_release_walk_the_declared_service_statuses` |
| контракт `POST /tasks` без типа и без статуса | `test_default_type_and_status_reproduce_pre_v08_behaviour` + неизменённые `test_tasks.py` |
| полный граф переходов системного типа | `test_system_lifecycle_allows_every_pre_v08_transition` (24 пары) |

## Изменения контракта

| Изменение | Влияние |
|---|---|
| `PATCH` со статусом, равным текущему, → `422 invalid_transition` | **единственное сужение**; раньше проходил и увеличивал версию |
| `GET /tasks?status=<неизвестный ключ>` → пустая страница вместо `422` | расширение: глобального словаря ключей больше нет |
| downgrade миграции схлопывает пользовательские ключи | необратимо; документировано в ADR-0048, SPEC §9 и docstring ревизии |

## Что не проверялось и почему

- **`field_schema` не применяется** к данным: колонка заведена пустой, валидация
  полей задачи — WI-3. Проверена только валидность самой схемы при создании
  типа;
- **производительность backfill** на большом объёме `tasks` не измерялась:
  окно обслуживания зафиксировано в `docs/migration-v0.8.md` по аналогии с
  `1adf50721f1e`;
- **ESLint фронтенда** не запускался: конфигурация в `web/` не инициализирована
  (`next lint` уходит в интерактивный мастер) — это состояние репозитория до
  задачи. Типы фронтенда проверены: `npx tsc --noEmit` — чисто.
