# TASK-000027: Custom fields, плановые даты и фильтрация — verification

Статус: completed

База: `main` (`6020306`).

Связанные документы: `docs/specs/TASK-000027-work-item-fields-dates.md`,
`docs/plans/TASK-000027-work-item-fields-dates.md`,
`docs/adr/0049-work-item-fields-dates-filters.md`, `docs/migration-v0.8.md`.

## Прогоны

| Проверка | Команда | Результат |
|---|---|---|
| Линт и формат | `uv run ruff check . && uv run ruff format --check .` | зелёный, 388 файлов |
| Типы | `uv run mypy src` | `Success: no issues found in 130 source files` |
| Полный набор | `uv run pytest` | **835 passed, 13 skipped** (было 795 до задачи; +40) |

| Файл | Тестов | Из них новых |
|---|---|---|
| `tests/unit/test_work_item_domain.py` | 59 | 14 (11 функций, одна параметризована 4 случаями) |
| `tests/integration/test_task_fields_v08.py` | 21 | 21 |
| `tests/client/test_mcp_tools.py` | 16 | 4 |
| `tests/integration/test_migration_v08.py` | 5 | 1 |

Правки в существующих тестах — только константы head миграции
(`test_migration_v05.py`, `test_migration_v07.py`: `c8a51d70b394` →
`a1c7e94b2f60`) и возврат роундтрипов на head, потому что ORM ожидает колонки
второй ревизии. Ни один существующий контрактный тест задач не менялся.

## Acceptance задачи

| # | Критерий | Чем закрыт | Статус |
|---|---|---|---|
| 1 | `custom_fields`, не проходящие `field_schema` типа, отклоняются | `test_custom_fields_are_validated_against_the_type_that_the_task_pins`, `test_updating_fields_is_checked_against_the_pinned_type_version`, 4 параметризованных доменных случая | ✅ |
| 2 | внешние `$ref` не резолвятся и не порождают сетевых обращений | `test_a_remote_ref_in_the_stored_schema_is_a_validation_error_not_a_fetch` (+ запрет на входе в реестр типов, TASK-000025) | ✅ |
| 3 | secret scan отклоняет попытку положить в поля секрет | `test_secrets_are_refused_in_custom_fields`, `test_secret_material_in_custom_fields_is_refused`, `test_an_opaque_secret_ref_is_still_allowed`, `test_secrets_in_custom_fields_are_refused_at_the_surface` (MCP) | ✅ |
| 4 | фильтры применяются до пагинации и совместимы с курсором | `test_a_filter_narrows_the_page_before_the_cursor_is_applied`, `test_date_paging_covers_every_row_exactly_once` | ✅ |
| 5 | сортировка по датам детерминирована при равных значениях | три задачи с одинаковым `dueDate`: обход страницами по 2 даёт 5 уникальных id ровно один раз (`test_date_paging_covers_every_row_exactly_once`) | ✅ |
| 6 | отсутствие `due_date` не ломает существующие выборки | `test_a_task_without_dates_is_the_default`, `test_due_date_ordering_is_soonest_first_with_undated_tasks_last` (задача без даты — в хвосте, а не потеряна); выборка по умолчанию не изменилась — прежние тесты `test_tasks.py` прошли без правок | ✅ |
| 7 | tenant isolation закреплена тестами | `test_dates_and_fields_do_not_cross_the_tenant_boundary` | ✅ |
| 8 | плановые даты типизированы и индексированы | ревизия `a1c7e94b2f60`; `test_fields_revision_is_additive_and_reversible` | ✅ |
| 9 | фильтры по типу, owner, категории и датам | `test_owner_filter`, `test_date_bounds_are_inclusive_and_skip_undated_tasks`, `test_start_date_bounds_are_independent_of_due_date`; тип и категория — TASK-000025 | ✅ |

## Дополнительно проверено (сверх acceptance)

- обратный интервал отклоняется приложением при создании и при обновлении
  одного конца, и БД (`ck_tasks_planned_dates_ordered`) —
  `test_a_backwards_interval_is_refused_on_create`,
  `test_moving_one_end_is_checked_against_the_stored_other_end`,
  `test_the_database_refuses_a_backwards_interval_too`;
- отклонённое обновление не двигает версию задачи (проверяется в том же тесте);
- содержимое `custom_fields` не попадает в журнал —
  `test_the_journal_records_that_fields_changed_but_not_what_they_are`;
- курсор чужого порядка → `invalid_cursor`, неизвестный `sort` →
  `invalid_sort`;
- MCP: установка и очистка одной даты в одном вызове отклоняются, задача
  остаётся версии 1.

## Риски и что осталось

| Риск | Оценка |
|---|---|
| Клиент, повторно использующий курсор при смене `sort` | Отклоняется явной ошибкой, а не молчаливой перестановкой страниц |
| Рост `tasks` от JSONB | Ограничен теми же 64 КиБ, что и конфигурация проекта |
| Индексы строятся не `CONCURRENTLY` | Короткая блокировка `tasks`, отмечено в migration-v0.8 |

Не входило в задачу и не сделано: отображение полей и дат в `web/`, фильтрация
по значению внутри `custom_fields`, метки, ранжирование, watchers.
