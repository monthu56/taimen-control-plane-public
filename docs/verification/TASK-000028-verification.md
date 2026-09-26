# TASK-000028: Комментарии к work item — verification

Статус: completed

База: `main` (`a5d2ca3`).

Связанные документы: `docs/specs/TASK-000028-work-item-comments.md`,
`docs/plans/TASK-000028-work-item-comments.md`,
`docs/verification/TASK-000028-threat-model.md`,
`docs/adr/0050-work-item-comments.md`, `docs/migration-v0.8.md`.

## Прогоны

| Проверка | Команда | Результат |
|---|---|---|
| Линт и формат | `uv run ruff check . && uv run ruff format --check .` | зелёный, 407 файлов |
| Типы | `uv run mypy src` | `Success: no issues found in 137 source files` |
| Полный набор | `uv run pytest` | **936 passed, 15 skipped**, из них 44 теста добавлены этой задачей |

Прогон делался в рабочем дереве, где параллельно велась TASK-000035 (адаптер
Claude Code), поэтому в общем счёте присутствуют и её тесты. На перечисленные
ниже критерии это не влияет: они закрыты поимённо названными тестами.
| Плагин оператора | `python3 -m unittest discover -s tests` в `operator-harness-template` | **30 passed** |

| Файл | Тестов | Из них новых |
|---|---|---|
| `tests/integration/test_task_comments_v08.py` | 21 | 21 |
| `tests/unit/test_work_item_domain.py` | 78 | 19 (5 функций, три параметризованы) |
| `tests/client/test_mcp_tools.py` | 19 | 3 |
| `tests/integration/test_migration_v08.py` | 6 | 1 |
| `tests/test_plugin.py` (operator-harness-template) | 30 | 2 |

Правки в существующих тестах — только константы head миграции
(`test_migration_v05.py`, `test_migration_v07.py`: `a1c7e94b2f60` →
`b8d3f1a45c72`) и две новые таблицы в `TRUNCATE` тестового `conftest.py`. Ни
один существующий контрактный тест не менялся.

## Acceptance задачи

| # | Критерий | Чем закрыт | Статус |
|---|---|---|---|
| 1 | комментарий не пересекает границу тенанта | `test_a_comment_never_crosses_a_tenant_boundary` (чтение ленты, чтение по id, запись → `404`); композитный FK `(tenant_id, task_id)` на уровне БД | ✅ |
| 2 | автор выводится из доверенного контекста, а не из тела запроса | `test_author_comes_from_the_credential_not_from_the_body`: реплики человека и агента несут разных Principal, поле автора в теле → `400 invalid_request` | ✅ |
| 3 | редактирование сохраняет предыдущую версию в audit | `test_edit_keeps_the_previous_version_as_a_revision`; ревизия пишется до нового текста; `test_revision_history_cannot_be_rewritten_in_the_database` — БД отклоняет UPDATE и DELETE | ✅ |
| 4 | secret scan отклоняет секреты в теле | `test_credential_shaped_text_is_refused` (5 форм), 9 доменных случаев, `test_a_comment_carrying_a_credential_is_refused_at_the_surface` (MCP); обратная сторона — `test_talking_about_credentials_is_still_allowed` и 5 доменных случаев | ✅ |
| 5 | пагинация устойчива к конкурентной вставке | `test_thread_reads_forward_and_survives_a_concurrent_insert`: реплика, добавленная между страницами, доставлена ровно один раз и ничего не сдвинула | ✅ |
| 6 | удаление или архивация задачи не теряет audit комментариев | удаления задачи в API нет; отмена — статус, а не удаление (`test_a_terminal_task_still_accepts_a_retro_note`); строки держатся FK на `tasks`, история — отдельной append-only таблицей, а ссылки на комментарии дополнительно живут в журнале событий | ✅ |
| 7 | события не содержат тела сверх утверждённой схемы | `test_the_journal_carries_references_and_never_the_body`: payload — `commentId`, `authorPrincipalId`, `version`, `bodyLength`, `runId`, `artifactId`; текст в журнал не попадает | ✅ |
| 8 | комментарий как отдельная сущность с автором, временем и версией | ревизия `b8d3f1a45c72`, `TaskCommentOut`; `test_comments_revision_is_additive_and_reversible` | ✅ |
| 9 | опциональная связь комментария с Run или Artifact | `test_an_attached_run_or_artifact_must_belong_to_the_same_task` — привязка к чужой работе → `422 comment_mismatch` | ✅ |

## Дополнительно проверено (сверх acceptance)

- правка требует `If-Match` (`428`) и отклоняет устаревшую версию (`409`) —
  `test_edit_requires_if_match_and_refuses_a_stale_one`;
- правка «в то же самое» не создаёт ни ревизии, ни версии, ни события —
  `test_a_no_op_edit_writes_no_history` (идемпотентный retry не производит
  историю);
- чужой автор не может править даже с `admin` —
  `test_only_the_author_may_edit_even_with_admin`, реплика остаётся версии 1;
- комментарий, адресованный через постороннюю задачу, — `404` и на чтении, и на
  правке (`test_a_comment_is_addressed_through_its_own_task`);
- курсор чужой выборки → `422 invalid_cursor`
  (`test_a_cursor_from_another_ordering_is_refused`);
- чтение требует `tasks.read`, запись — `tasks.write`
  (`test_reading_a_thread_requires_read_and_writing_requires_write`);
- завершённая задача принимает ретро-заметку
  (`test_a_terminal_task_still_accepts_a_retro_note`);
- плагин оператора: `cp_comment` отклоняется под read-only binding, а
  `cp_list_comments` — нет (`test_read_only_binding_denies_a_comment`,
  `test_reading_a_thread_is_allowed_under_a_read_only_binding`).

## Риски и что осталось

| Риск | Оценка |
|---|---|
| Секрет неизвестного формата пройдёт скан | Guard от честной ошибки, не сканер; смещение в сторону пропуска выбрано сознательно, чтобы не глушить обсуждение (ADR-0050) |
| Transcript или reasoning в теле | Механического критерия нет; держится лимитом 10 000 символов и запретом в контракте оператора и описаниях MCP-инструментов |
| Невозможность удалить комментарий | Осознанно: удаление — единственный способ убрать сказанное без следа. Изъятие данных — операция уровня тенанта, а не координации |
| Третий формат курсора в API | Клиент обязан возвращать курсор туда, откуда его получил; нарушение — явная ошибка, а не тихо неверная страница |

Не входило в задачу и не сделано: отображение треда в `web/`, реакции,
упоминания и уведомления (зависят от Channel Service), вложения,
полнотекстовый поиск, модераторская правка чужого текста.
