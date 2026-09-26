# ADR-0032: Версионированная конфигурация проекта и детерминированный effective config

Статус: Принято (v0.5)

## Контекст

Конфигурация проекта меняется во времени, влияет на исполнение и должна быть
аудируемой. Одновременно она наследуется по дереву: портфель задаёт рамку,
проект её уточняет. Нужен порядок слоёв, который не зависит от порядка строк в
базе и объясняет, откуда взялось каждое значение.

## Решение

### Документ конфигурации

`config` — JSON-объект ровно с четырьмя разделами; посторонние ключи
отклоняются:

```json
{
  "settings":   {},
  "views":      [],
  "governance": {},
  "memory":     {},
  "inheritance": {"inheritableSettings": ["*"], "lockedSettings": []}
}
```

`inheritance` объявляется только в `default_config` шаблона и в ревизиях; она
определяет, какие ключи `settings` протекают в дочерние проекты и какие ключи
потомок не имеет права переопределять.

### Append-only ревизии

`project_config_revisions`: `(project_id, revision)` уникальна, `revision`
монотонна и назначается под row lock проекта. Строка неизменяема с момента
создания — триггер БД запрещает DELETE и любой UPDATE, кроме перевода
`activated_at` из NULL в значение.

Создание ревизии **не** активирует её. Активация — отдельная транзакционная
команда `POST /projects/{id}/config-revisions/{revision}:activate` с
обязательным `If-Match` на версию проекта и row lock профиля. Авторитетный
указатель ровно один: `project_profiles.active_config_revision_id`. Состояние,
событие и outbox пишутся в одной транзакции; повтор с тем же `Idempotency-Key`
возвращает тот же результат.

Невалидная ревизия не сохраняется: JSON Schema, lifecycle и governance
проверяются до INSERT.

### Порядок слоёв effective config

Слои применяются строго в этом порядке, и порядок зафиксирован тестовой
таблицей:

1. `default_config` точной версии Template;
2. разрешённые `settings` ближайших ancestor Projects — от корня к родителю
   (порядок обхода фиксирован глубиной в дереве Workspace, а не порядком строк);
3. активная config revision проекта;
4. `project_profiles.settings` — типизированный overlay профиля.

Правила слияния по разделам:

| Раздел | Семантика |
|---|---|
| `settings` | рекурсивное слияние объектов; массивы и скаляры заменяются целиком |
| `views` | замена целиком (порядок представлений — единое целое) |
| `governance` | свёртка «строже» (ADR-0033), а не замена |
| `memory` | как `settings` |
| `inheritance` | берётся из ближайшего объявившего слоя; `lockedSettings` накапливаются объединением по всем предкам |

Ключ, заблокированный `lockedSettings` любого предка, нельзя переопределить ни
ревизией, ни `settings` профиля: попытка отклоняется до commit с
`422 setting_locked` и `details.path`.

Ответ `GET /projects/{id}/effective-config` содержит и результат, и provenance:
для каждого верхнеуровневого ключа каждого раздела — слой-источник (`template`,
`ancestor`, `revision`, `profile`), идентификатор проекта-предка и номер
ревизии. Effective config всегда считается на сервере; клиентское значение не
принимается ни в каком виде.

## Последствия

- Рекурсивное слияние объектов означает, что «удалить унаследованный вложенный
  ключ» напрямую нельзя — нужно переопределить весь объект-контейнер (замена
  массива/скаляра). Это цена детерминизма; явного sentinel-удаления в v0.5 нет.
- Ревизии накапливаются и не удаляются: это журнал изменений конфигурации.
  Retention для них в v0.5 не вводится.
- Один авторитетный указатель на активную ревизию делает конкурентную активацию
  двух ревизий разрешимой ровно одной победившей транзакцией (проверено
  concurrency-тестом).
- Provenance по верхнеуровневым ключам, а не по каждому листу вложенного
  объекта: полная провенанс-карта листьев удвоила бы размер ответа, а
  практическая ценность — на уровне ключа.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "migrations/versions/*.py", pattern: 'project_config_revisions rows are immutable'}
  repo: control-plane
- route: "POST /projects/{project_id}/config-revisions/{revision}:activate"
  repo: control-plane
- route: "GET /projects/{project_id}/effective-config"
  repo: control-plane
- grep: {path: src/control_plane/domain/project.py, pattern: 'def compute_effective_config'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/projects.py, pattern: '"setting_locked"'}
  repo: control-plane
```
