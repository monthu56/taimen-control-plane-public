# ADR-0013. Artifact: reference-модель хранения

Статус: принято (2026-08-11, v0.2); амендмент 2026-09-26 —
[ADR-0072](0072-artifact-content-types-task-io.md) (содержимое в хранилище ядра)

## Контекст

Результаты работы (PR, документы, отчёты, датасеты) должны быть first-class
записями с аудитом, но PostgreSQL не должен превращаться в object storage.

## Решение

- `artifacts` — **append-only**: только create/get/list; ни UPDATE, ни DELETE
  API не существует. Ревизия = новая запись (metadata может ссылаться на
  предшественника).
- Большие/бинарные данные живут во внешних системах; артефакт хранит `uri` +
  `metadata`. Небольшой встроенный JSON-контент допустим в `content`
  (ограничен глобальным лимитом тела запроса, 1 MiB).
- Связи опциональны: workspace, task, run. Артефакт, привязанный к run,
  наследует его задачу; несоответствие run/task — `422 artifact_mismatch`.
- Событие `artifact.created` несёт только ссылки (type, name, ids, uri) —
  **content в журнал не пишется** (объём + не место для содержимого в аудите).
- `type` — свободная доменная строка (`github.pull_request`, `contract.review`);
  реестра типов в v0.2 нет.

## Последствия

- История артефактов задачи/run'а неизменна и полна независимо от takeover'ов.
- Сборка мусора внешних объектов — ответственность внешнего хранилища (v2).

## Амендмент 2026-09-26 (CP-ADR-0072): содержимое в хранилище ядра

Фича `artifact-handoff` (TASK-000518). Решение «большие/бинарные данные
живут во внешних системах» и «реестра типов нет» уточняются
[ADR-0072](0072-artifact-content-types-task-io.md):

- Артефакт — **либо ссылка** на систему учёта клиента (`uri`, ядро
  содержимым не владеет и не копирует его), **либо содержимое в хранилище
  ядра** (`contentRef`: S3-совместимое хранилище в контуре установки через
  порт `ContentStore`, объект по sha256 внутри tenant), либо, как раньше,
  небольшой JSON `content`. PostgreSQL по-прежнему не хранит байты: в записи —
  `sizeBytes`, `mediaType`, `sha256`, `contentState`.
- Байты загружаются и выдаются только через API ядра (`PUT
  /artifact-contents`, `GET /artifacts/{id}/content`) с авторизацией на задаче
  артефакта; каждая выдача — событие `artifact.content_read`.
- Append-only сохраняется: записи не меняются и не удаляются.
  Единственное исключение для **байтов** — `POST
  /artifacts/{id}:purge-content` администратора tenant: запись остаётся с
  `contentState = purged` и событием `artifact.content_purged`. `PUT`/`PATCH`/
  `DELETE` на `/artifacts` по-прежнему нет (загрузка — отдельный ресурс
  `/artifact-contents`).
- Сборка мусора объектов ядра — worker (загрузки без артефакта старше 24 ч);
  объекты внешних систем — по-прежнему их ответственность.
- `type` остаётся свободной строкой; зарегистрированный тип артефакта (вид
  каталога `ArtifactType`) добавляет проверку метаданных, media type и
  размера, незарегистрированные работают как раньше.
- `artifact.created` по-прежнему без содержимого; схема v2 добавляет
  `sizeBytes`, `mediaType`, `sha256`, `contentState`, `typeVersion`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- route: "POST /artifacts"
  repo: control-plane
- absent: {path: src/control_plane/api/v1/artifacts.py, pattern: '@router\.(put|patch|delete)\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/artifacts.py, pattern: '"artifact_mismatch"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/artifacts.py, pattern: 'The event carries references only'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'content: Mapped\[dict\[str, Any\] \| None\] = mapped_column\(JSONB'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/artifacts/\{[a-z_]+\}:purge-content"'}
  repo: control-plane
```
