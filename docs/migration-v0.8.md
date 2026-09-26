# Миграция v0.7 → v0.8: тип work item, поля, даты и комментарии

Ревизии линии v0.8, по порядку:

| Ревизия | Задача | Что делает |
|---|---|---|
| `c8a51d70b394` (revises `f5b91c3e7a24`) | TASK-000025 | реестр `task_types`, тип и категория статуса у задачи |
| `a1c7e94b2f60` (revises `c8a51d70b394`) | TASK-000027 | `custom_fields`, `start_date`, `due_date` и индексы под фильтры |
| `b8d3f1a45c72` (revises `a1c7e94b2f60`) | TASK-000028 | таблицы `task_comments` и append-only `task_comment_revisions` |

TASK-000026 (обобщение external references, ADR-0047) схему не меняла.
Ветвления heads нет.

Текущий head: `b8d3f1a45c72`.

## Что добавлено

- таблица `task_types` — tenant-scoped версионируемый реестр с `field_schema`
  и `lifecycle_schema`, иммутабельность версии обеспечивает триггер
  `forbid_task_type_mutation` (только `active -> deprecated`);
- колонки `tasks.type_id` (NOT NULL, tenant-consistent FK) и
  `tasks.system_status_category` (NOT NULL, закрытый словарь из пяти значений);
- индексы `ix_tasks_tenant_category` и `ix_tasks_type`;
- права `task_types.read` и `task_types.manage`;
- эндпоинты `/task-types` (create version, list, get, `:deprecate`);
- поля `typeId`, `typeKey`, `typeVersion`, `systemStatusCategory` в `TaskOut`;
- фильтры `?systemStatusCategory=` и `?typeKey=` в `GET /tasks`;
- события `task_type.created|deprecated`.

## Что изменено в схеме

`CHECK (status IN ('backlog','todo','in_progress','blocked','done','cancelled'))`
снят. Вместо него `CHECK (char_length(status) BETWEEN 1 AND 64)` и отдельный
`CHECK` на категорию. Словарь статусов теперь живёт в `lifecycle_schema` типа и
проверяется приложением под блокировкой строки задачи — там же, где
проверяется статус профиля проекта.

## Данные

1. Каждому тенанту создаётся системный тип `task` версии 1 с шестью прежними
   статусами и графом переходов, полным на четырёх нетерминальных.
2. Каждая существующая задача получает этот тип и категорию по отображению:

   | Статус | Категория |
   |---|---|
   | `backlog` | `backlog` |
   | `todo` | `active` |
   | `in_progress` | `active` |
   | `blocked` | `blocked` |
   | `done` | `terminal_success` |
   | `cancelled` | `terminal_cancelled` |

3. **Статусы не переписываются, claimability не меняется.** До миграции
   claimable означало «не `done` и не `cancelled`»; после — «категория не
   терминальная». На отображении выше это одно и то же множество задач, включая
   задачи в `blocked`.

Новые тенанты получают системный тип в `bootstrap`, как и системный workspace
type.

## Совместимость API

- `status` остаётся ключом статуса во всех запросах и ответах; категория
  добавлена рядом, а не вместо;
- `POST /tasks` без `typeKey` резолвится в системный тип, без `status` — в
  `initialStatus` типа (`todo`). Прежние клиенты работают без изменений;
- единственное сужение: `PATCH` со статусом, равным текущему, теперь
  `422 invalid_transition`. Раньше он проходил и увеличивал версию.

## Downgrade

**Аварийный путь, не штатный откат.** Шестизначный `CHECK` не примет статус,
который тенант ввёл после апгрейда, поэтому downgrade схлопывает каждый такой
ключ на легаси-ключ его категории: `backlog → backlog`, `active → todo`,
`blocked → blocked`, `terminal_success → done`, `terminal_cancelled →
cancelled`. Исходные ключи теряются безвозвратно; версия задачи и журнал
событий не трогаются, поэтому потеря видна в истории.

## Эксплуатация

Backfill трогает каждую строку `tasks`, индексы строятся не `CONCURRENTLY`
(Alembic держит DDL в транзакции) — планируйте окно, как для `1adf50721f1e`.

## `a1c7e94b2f60` — поля и плановые даты (TASK-000027, ADR-0049)

Ревизия аддитивная: три колонки на `tasks` и три индекса, данные не
переписываются.

- `custom_fields` (JSONB, default `{}`) — проверяется при записи по
  `field_schema` типа, который закрепила задача. У системного типа схема пустая
  и принимает всё, поэтому поведение существующих задач не меняется;
- `start_date`, `due_date` (`timestamptz`, NULL) и
  `CHECK ck_tasks_planned_dates_ordered` — для существующих строк ограничение
  тривиально истинно (обе даты NULL);
- индексы `ix_tasks_tenant_owner`, частичные `ix_tasks_tenant_due` и
  `ix_tasks_tenant_start` (только строки с датой).

Совместимость API: все поля опциональны, `TaskOut` пополняется `customFields`,
`startDate`, `dueDate`. Прежние клиенты не меняются. Новое сужение — только у
`PATCH`: `customFields: null` отклоняется (`invalid_field`), очистка — `{}`.

Downgrade обычный, но лоссовый: колонки удаляются вместе с содержимым, схема
типов не затрагивается.

ADD COLUMN'ы не переписывают таблицу (все NULL-able или с server default),
индексы строятся не `CONCURRENTLY` — короткая блокировка `tasks`.

## `b8d3f1a45c72` — комментарии и история правок (TASK-000028, ADR-0050)

Ревизия строго аддитивная: две новые таблицы, ни одна существующая не
изменяется и ни одна строка не переписывается, поэтому её можно применять при
работающем API.

- `task_comments` — реплика в треде work item. Автор — Principal, тело
  ограничено `CHECK char_length(body) BETWEEN 1 AND 10000`, композитный FK
  `(tenant_id, task_id)` держит комментарий и задачу в одном тенанте на уровне
  БД. `CHECK (version = 1) = (edited_at IS NULL)` не даёт разойтись версии и
  признаку правки. Индекс `ix_task_comments_thread` несёт восходящую пару
  `(created_at, id)` — ровно ту, что сравнивает курсор ленты;
- `task_comment_revisions` — прежние версии текста. Append-only: триггеры
  `task_comment_revisions_append_only` и `..._truncate` отклоняют UPDATE,
  DELETE и TRUNCATE так же, как это сделано для `events`;
- новых прав нет: чтение — `tasks.read`, запись — `tasks.write`.

Совместимость API: чистое добавление эндпоинтов
`/tasks/{ref}/comments[/{id}[/revisions]]`. Существующие контракты и поля не
меняются; ни один прежний клиент не затрагивается.

Downgrade обычный, но лоссовый: обе таблицы удаляются вместе с тредами и их
audit. Ничто другое от них не зависит, поэтому откат в остальном чистый.

Эксплуатация: `CREATE TABLE` не блокирует `tasks`; FK на `tasks`, `runs`,
`artifacts` и `principals` берут короткую `SHARE ROW EXCLUSIVE` на этих
таблицах на время создания.

## Проверка

`tests/integration/test_migration_v08.py` — roundtrip `upgrade → downgrade →
upgrade` на непустой базе, сохранение статусов, claimability задачи в
`blocked`, схлопывание пользовательского ключа при downgrade, аддитивность и
обратимость ревизий полей и комментариев.

`tests/integration/test_task_fields_v08.py` — контракт полей, дат, фильтров и
порядка чтения; `tests/integration/test_task_comments_v08.py` — авторство,
аудит правок, порядок ленты и секретный скан;
`tests/unit/test_work_item_domain.py` — доменные правила.
