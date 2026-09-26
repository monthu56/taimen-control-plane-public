# ADR-0030: Неизменяемые версионированные Project Templates

Статус: Принято (v0.5)

## Контекст

Project Template задаёт custom fields, lifecycle, governance, представления и
memory-политику проекта. Проект ссылается на шаблон, и от шаблона зависит
валидность его данных. Если шаблон менять на месте, ранее сохранённые
`custom_fields` могут перестать соответствовать схеме, а история изменений
конфигурации станет невоспроизводимой.

Кандидаты: (1) mutable шаблон с миграцией данных при каждом изменении;
(2) draft/published lifecycle с иммутабельностью после публикации;
(3) полная иммутабельность версии с момента создания.

## Решение

Выбран вариант (3) — самый сильный и самый простой контракт.

- Ключ версии: `(tenant_id, key, version)` уникален. `version` — целое,
  монотонное в пределах `(tenant_id, key)`, назначается сервером под advisory
  lock; клиент версию не выбирает.
- Строка версии шаблона неизменяема с момента создания. Единственная разрешённая
  мутация — переход `status: active → deprecated` через `POST
  /project-templates/{id}:deprecate`. Иммутабельность содержимого обеспечивает
  триггер базы, а не только слой приложения.
- Изменение шаблона — это `POST /project-templates` с тем же `key`: создаётся
  новая версия. Draft-состояния нет: незавершённый шаблон просто не используется.
- `Project` ссылается на конкретную строку `project_templates.id`, то есть на
  точную версию. Смена версии шаблона у проекта — явная операция (PATCH с
  `templateId`), которая перевалидирует `custom_fields`, `status_key` и активную
  config revision по новой схеме и отклоняется целиком, если состояние проекта
  становится невалидным.
- `deprecated` шаблон нельзя выбрать для нового проекта; существующие проекты
  продолжают работать без изменений.
- `field_schema`, `lifecycle_schema` и `governance_schema` валидируются до
  сохранения. Ошибки валидации имеют стабильные машиночитаемые коды и JSON-путь
  (`details.path`).

## Последствия

- «Опечатка в шаблоне» стоит одной новой версии, а не миграции данных. Это
  осознанная цена: справочник версий растёт.
- Никакой шаблон нельзя «незаметно поправить» под работающими проектами — это и
  есть цель.
- Список версий одного `key` может стать длинным; `GET /project-templates`
  поддерживает фильтр `key` и по умолчанию отдаёт все версии в стабильном
  порядке `(created_at, id)`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("tenant_id", "key", "version", name="uq_project_templates_key_version"'}
  repo: control-plane
- grep: {path: "migrations/versions/*.py", pattern: 'CREATE TRIGGER project_templates_immutable'}
  repo: control-plane
- route: "POST /project-templates/{template_id}:deprecate"
  repo: control-plane
```
