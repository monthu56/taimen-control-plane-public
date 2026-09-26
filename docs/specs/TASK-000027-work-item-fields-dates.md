# TASK-000027: Custom fields, плановые даты и расширенная фильтрация — SPEC

Статус: implemented

Источники: `docs/adr/ADR-0015-work-item-model.md` (верхний уровень, «Решение»
п.3, 4, 7 и «Границы»), `docs/adr/0049-work-item-fields-dates-filters.md`,
прецеденты — `docs/adr/0031-project-profile-and-lifecycle.md` и
`docs/adr/0048-work-item-type-and-lifecycle.md`.

## 1. Проблема

`task_types.field_schema` заведён TASK-000025 пустой колонкой и намеренно не
применялся ни к чему: тип описывал словарь статусов, но не описывал поля. У
задачи не было ни расширяемых полей, ни плановых дат; `GET /tasks` не умел
фильтровать по owner и датам и знал один порядок — `(created_at, id)` по
убыванию.

## 2. Модель данных

```
tasks.custom_fields  JSONB   NOT NULL DEFAULT '{}'
tasks.start_date     timestamptz NULL
tasks.due_date       timestamptz NULL

CHECK ck_tasks_planned_dates_ordered:
    start_date IS NULL OR due_date IS NULL OR start_date <= due_date

INDEX ix_tasks_tenant_owner (tenant_id, owner_id)
INDEX ix_tasks_tenant_due   (tenant_id, due_date, id)   WHERE due_date IS NOT NULL
INDEX ix_tasks_tenant_start (tenant_id, start_date, id) WHERE start_date IS NOT NULL
```

Индексы по датам частичные: большинство work item дат не несёт, и полный индекс
был бы почти целиком из NULL. Хвост `id` обязателен — постраничный обход по дате
сравнивает пару.

Ревизия `a1c7e94b2f60` (revises `c8a51d70b394`) аддитивна: ADD COLUMN не
переписывает таблицу, CHECK тривиально истинен на существующих строках.

## 3. Валидация custom fields

Порядок фиксирован и важен:

1. `validate_against_schema` — внутри `guard_json_document` (≤64 КиБ, ≤20
   уровней, ≤5000 узлов) **до** движка схем: патологический документ не должен
   жечь CPU внутри валидатора;
2. JSON Schema draft 2020-12 против `field_schema` **той версии типа, которую
   закрепила задача**; ошибки отдаются списком путей, код
   `custom_fields_invalid`;
3. `reject_secret_material` — ключи вида `password`, `token`, `apiKey`,
   `secret`, `credential`; `secretRef` разрешён.

Внешние `$ref` в схеме запрещены на входе в реестр типов
(`validate_json_schema_document`), а если бы такая схема оказалась в хранилище,
проверка экземпляра падает как `invalid_json_schema` — без сетевого обращения.

Пустая схема принимает всё: системный тип и все задачи, созданные до этой
задачи, ведут себя как раньше.

Секретный скан на custom fields — сознательное отличие от Project Profile, где
он покрывает только `settings`: задачу правят агенты, и это более вероятное
место для случайно вставленного токена.

## 4. Семантика записи

| Операция | Поведение |
|---|---|
| `POST /tasks` | `customFields` (default `{}`), `startDate`, `dueDate` опциональны |
| `PATCH` `customFields: {...}` | замена документа целиком |
| `PATCH` `customFields: null` | `422 invalid_field` — очистка это `{}` |
| `PATCH` `startDate: null` | очищает дату |
| `PATCH` одного конца интервала | сверяется с **сохранённым** вторым концом |

Merge для `customFields` не вводится: частичный документ нельзя проверить схемой
с `required`, а merge не позволяет удалить ключ.

Naive datetime читается как UTC (`normalize_planned_date`) — иначе сохранённый
момент зависел бы от таймзоны сессии сервера, а сравнение `start <= due`
смешивало бы aware и naive.

Обратный интервал → `422 invalid_planned_dates`; отказ происходит до любой
мутации, поэтому версия задачи не меняется. БД держит тот же инвариант
CHECK'ом.

## 5. Чтение

Новые query-параметры `GET /tasks`:

```
?ownerId=<uuid>
&startFrom=<iso>&startTo=<iso>&dueFrom=<iso>&dueTo=<iso>
&sort=createdAt|startDate|dueDate
```

Границы включающие. Ограничение по дате само исключает задачи без неё: NULL не
удовлетворяет сравнению.

`sort=dueDate|startDate` — `ORDER BY date ASC NULLS LAST, id ASC`. Порядок
тотальный: у команды с общим сроком даты равны, и без тай-брейка по `id`
страницы теряли бы и дублировали строки. Задачи без даты не исчезают — они в
хвосте; отсечь их — работа фильтра.

Курсор даты имеет собственный ключ (`{"d": iso|null, "i": id}`), поэтому курсор
другого порядка отклоняется как `422 invalid_cursor`, а не пагинирует молча
неверно. Внутри NULL-хвоста курсор несёт `d: null` и продолжает обход по `id`.

Неизвестный `sort` → `422 invalid_sort`. Все фильтры входят в statement до
курсорного предиката (ADR-0035).

## 6. События

`task.created` несёт `startDate`, `dueDate` и **булев** `customFields`;
`task.updated` — `changes.custom_fields: true`. Содержимое полей в журнал не
попадает: журнал читают шире, чем саму задачу (ADR-0015).

## 7. Поверхности

- `control_plane_client`: `create_task(custom_fields, start_date, due_date)`,
  `update_task(...)` через тот же `_UNSET`-гейт (явный `None` осмыслен для дат),
  `list_tasks(owner_id, start_from, start_to, due_from, due_to, sort)`.
- MCP: те же параметры у `cp_create_task` / `cp_list_tasks` / `cp_update_task`.
  Так как в MCP `None` значит «не трогать», очистка даты выражена флагами
  `clear_start_date` / `clear_due_date`; одновременная установка и очистка одной
  даты — `invalid_request` без записи.

## 8. Вне объёма

Фильтрация по значению внутри `custom_fields`, метки отдельной сущностью,
ручное ранжирование для kanban, полнотекстовый поиск, watchers, отображение
полей и дат в `web/`.
