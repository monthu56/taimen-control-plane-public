# ADR-0013. Artifact: reference-модель хранения

Статус: принято (2026-08-11, v0.2)

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
```
