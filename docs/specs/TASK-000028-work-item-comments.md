# TASK-000028: Комментарии к work item — SPEC

Статус: implemented

Источники: `docs/adr/ADR-0015-work-item-model.md` (верхний уровень, «Решение»
п.6 и «Границы»), `docs/adr/0050-work-item-comments.md`; прецеденты —
`docs/adr/0020-artifact-revisions.md` и
`docs/adr/0049-work-item-fields-dates-filters.md`.

## 1. Проблема

Добавить что-либо к задаче можно было двумя способами, и оба неверны. Переписать
`description` — потерять авторство и прежний текст. Создать Artifact — выдать
реплику за результат работы. Ленты обсуждения с авторством не существовало,
поэтому причина решения, вопрос человеку и заметка следующему исполнителю жили
в transcript'ах харнессов, то есть вне авторитетного состояния.

Для пилота автономного исполнителя это дыра в самом нужном месте: человек и
агент работают над одной задачей, и без ленты с авторством нельзя ни
восстановить, кто что решил, ни передать работу без переизложения контекста.

## 2. Модель данных

```
task_comments
    id                  uuid PK
    tenant_id           uuid -> tenants.id
    task_id             uuid
    author_principal_id uuid -> principals.id
    body                text
    run_id              uuid NULL -> runs.id
    artifact_id         uuid NULL -> artifacts.id
    version             int  NOT NULL
    created_at          timestamptz
    updated_at          timestamptz
    edited_at           timestamptz NULL

CHECK ck_task_comments_body_length:              char_length(body) BETWEEN 1 AND 10000
CHECK ck_task_comments_version_positive:         version >= 1
CHECK ck_task_comments_edited_at_matches_version:(version = 1) = (edited_at IS NULL)
FK   fk_task_comments_task: (tenant_id, task_id) -> (tasks.tenant_id, tasks.id)
INDEX ix_task_comments_thread (tenant_id, task_id, created_at, id)
INDEX ix_task_comments_author (tenant_id, author_principal_id)

task_comment_revisions
    id                  uuid PK
    tenant_id           uuid -> tenants.id
    comment_id          uuid -> task_comments.id
    task_id             uuid
    version             int         -- версия, которой ЭТА строка является
    body                text
    author_principal_id uuid -> principals.id
    created_at          timestamptz -- когда эта версия была написана
    superseded_at       timestamptz -- когда она перестала быть текущей
    superseded_by       uuid -> principals.id

UNIQUE uq_task_comment_revisions_version (comment_id, version)
TRIGGER task_comment_revisions_append_only           BEFORE UPDATE OR DELETE
TRIGGER task_comment_revisions_append_only_truncate  BEFORE TRUNCATE
```

Композитный FK повторяет приём `task_relations`: комментарий и его задача
принадлежат одному тенанту на уровне БД, а не только по договорённости
приложения. Индекс треда несёт **восходящую** пару `(created_at, id)` — ровно
ту, что сравнивает курсор ленты.

`run_id` и `artifact_id` ограничены той же задачей **командой**, а не схемой:
nullable композитный FK не умеет выразить «та же задача, что у комментария».

Ревизия `b8d3f1a45c72` (revises `a1c7e94b2f60`) строго аддитивна: две новые
таблицы, ни одна существующая не изменяется.

## 3. Авторство

`author_principal_id = ctx.principal_id`. Поля автора в
`TaskCommentCreateRequest` нет вовсе, а `ApiModel` объявлен с `extra="forbid"`,
поэтому попытка назвать автора в теле отклоняется контрактом (`400
invalid_request`) и до команды не доходит.

Отсюда следует главное свойство ленты: реплика агента отличается от реплики
человека строкой в базе, а не конвенцией в тексте.

## 4. Правка

- только автор; `admin` — не исключение (`403 not_comment_author`);
- обязателен `If-Match: "comment-<version>"` (`428 if_match_required`,
  `400 invalid_if_match`, `409 version_conflict`);
- строка комментария берётся `SELECT ... FOR UPDATE`, поэтому проверка версии и
  запись ревизии происходят под одной блокировкой;
- **ревизия пишется и флашится до** изменения текущего текста: если
  append-only история не записалась, правки не происходит;
- правка «в то же самое» (после `strip`) — no-op: ни ревизии, ни версии, ни
  события. Идемпотентный retry не должен производить историю правок;
- удаления комментария нет.

## 5. Тело комментария

`validate_comment_body`:

1. `strip` — хранится нормализованный текст, поэтому различие в невидимых
   символах не читается как новая версия;
2. пусто → `422 invalid_comment_body`;
3. > 10 000 символов → `422 payload_too_large` (контракт отсекает раньше:
   `400 invalid_request`);
4. `reject_secret_text` → `422 secret_material_rejected`.

Секретный скан по **тексту**, не по ключам: в прозе ключа нет, а говорить *о*
секретах разрешено. Ловится материал — `sk-…`, `gh[pousr]_…`, `xox[baprs]-…`,
`AKIA…`, `AIza…`, `Bearer <20+>`, JWT, PEM-заголовок приватного ключа, и
присваивание `password|secret|token|api_key|…` со значением длиной ≥16 без
пробелов. «Ротируем API key до пятницы» проходит; `password = correcthorse…`
нет.

Границу «не место для transcripts, raw prompts и chain-of-thought» механически
не проверить. Она держится лимитом длины, секретным сканом на самое опасное и
явным запретом в контракте оператора и в описаниях MCP-инструментов.

## 6. Чтение

`GET /tasks/{ref}/comments` — **от старых к новым**, единственная такая выборка
в API. Курсор сравнивает `(created_at, id)` строго больше, поэтому реплика,
написанная между запросами страниц, приезжает на следующей странице, а не
теряется перед первой (что и произошло бы при порядке «новые сверху»).

Курсор имеет собственный ключ (`a`), как курсор дат имеет свой (`d`): курсор
чужой выборки обязан отклоняться (`422 invalid_cursor`), а не пагинировать
молча другое.

`GET /tasks/{ref}/comments/{id}` отдаёт `ETag: "comment-<version>"`.
`GET /tasks/{ref}/comments/{id}/revisions` — та же восходящая пагинация по
истории правок.

Все три запроса скоупятся по тенанту **и** по задаче из пути: комментарий,
адресованный через чужую задачу, — `404`, потому что задача входит в его
идентичность.

## 7. События

`task.comment_added` и `task.comment_edited` — по потоку **задачи**
(`entityType: task`, `entityId: taskId`), чтобы подписчик work item видел
обсуждение там же, где смену статуса.

Payload: `commentId`, `authorPrincipalId`, `version`, `bodyLength`, `runId`,
`artifactId`. Тела нет — журнал реплицируется дальше, чем строка, которую он
описывает.

## 8. Права

Чтение — `tasks.read`, запись — `tasks.write`. Новых прав нет: комментарий
привязан ровно к одному work item и не несёт собственной авторитетности, а
отдельное право пришлось бы выдать каждой существующей роли, прежде чем
кто-нибудь смог бы заговорить.

## 9. Поверхности

| Слой | Что добавлено |
|---|---|
| HTTP | `POST/GET /tasks/{ref}/comments`, `GET/PATCH /tasks/{ref}/comments/{id}`, `GET .../revisions` |
| SDK | `add_task_comment`, `list_task_comments`, `get_task_comment`, `edit_task_comment`, `list_task_comment_revisions` |
| MCP | `cp_list_comments` (read-only), `cp_comment`, `cp_edit_comment` (mutating) |
| Плагин оператора | `cp_comment` / `cp_edit_comment` в `MUTATING_TOOLS`; раздел о границе комментария в контракте и SKILL |

## 10. Вне объёма

Реакции, упоминания и уведомления (зависят от Channel Service), вложения,
полнотекстовый поиск, удаление комментария, модераторская правка чужого текста,
отображение треда в web-интерфейсе.
