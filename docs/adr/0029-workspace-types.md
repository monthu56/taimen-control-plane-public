# ADR-0029: Workspace Types как типизированное ограничение дерева

Статус: Принято (v0.5)

## Контекст

v0.5 должна описывать портфели, программы, проекты, подпроекты, рабочие потоки и
команды. Все они — узлы одного дерева Workspace (ADR-0008), но правила вложенности
у них разные: команда внутри портфеля осмысленна, портфель внутри команды — нет.
Захардкодить конкретные типы в core нельзя: core остаётся product-neutral.

Рассмотренные варианты: (1) свободное дерево без типов — тогда структуру нельзя
проверить; (2) plugin system с исполняемыми правилами — избыточно и небезопасно;
(3) tenant-scoped справочник типов с декларативным списком допустимых детей.

## Решение

Вводится tenant-scoped таблица `workspace_types`: `key`, `display_name`,
`field_schema` (JSON Schema для расширяемых полей Workspace),
`allowed_child_types` (массив ключей), `status`, `is_system`, `version`.

- `key` уникален в пределах Tenant.
- `allowed_child_types` — массив ключей типов; сентинел `"*"` означает «любой тип»,
  пустой массив — «детей быть не может».
- Правило проверяется при create Workspace, при `:move` и при смене типа
  Workspace: тип родителя должен разрешать тип ребёнка. Проверка выполняется под
  тем же per-tenant advisory lock, что и остальные структурные мутации, поэтому
  гонка «оба ребёнка проходят проверку» невозможна.
- `field_schema` валидируется как JSON Schema при записи типа и применяется к
  `custom_fields` Workspace.
- Системный тип `generic` (`is_system = true`, `allowed_child_types = ["*"]`)
  создаётся для каждого Tenant миграцией и при bootstrap; все существующие
  Workspace получают его backfill'ом. Системный тип нельзя удалить или
  архивировать.
- Архивирование типа запрещено, пока на него ссылается активный Workspace, —
  дерево не может остаться невалидным.

`allowed_child_types` намеренно ссылается на `key`, а не на `id`: правило читаемо
в конфиге и переживает пересоздание справочника при миграции данных.

## Последствия

- Совместимость v0.4 полная: старые Workspace получают системный тип, а он
  разрешает любых детей — ни один существующий вызов не ломается.
- Ужесточение `allowed_child_types` не переваливает уже существующее дерево в
  невалидное состояние ретроактивно: правило проверяется только при мутациях.
  Это осознанный компромисс — иначе изменение типа требовало бы полного обхода
  поддерева в той же транзакции.
- Workspace Types — справочник, а не расширение поведения: они не несут кода,
  хуков и обработчиков. Любая попытка выразить в них логику должна вместо этого
  идти в Project Template (ADR-0030).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "workspace_types"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspace_types.py, pattern: 'SYSTEM_TYPE_KEY = "generic"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspace_types.py, pattern: '"child_type_not_allowed"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspace_types.py, pattern: '"workspace_type_in_use"'}
  repo: control-plane
- grep: {path: tests/integration/test_workspace_types_v05.py, pattern: 'def test_move_under_forbidding_parent_rejected'}
  repo: control-plane
```
