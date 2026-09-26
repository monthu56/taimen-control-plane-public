# ADR-0033: Типизированная решётка governance и запрет ослабления

Статус: Принято (v0.5)

## Контекст

Политика предка задаёт верхнюю границу полномочий: дочерний проект может
ужесточить ограничения, но не ослабить их. Чтобы это проверять, нужно уметь
сравнивать две политики. Произвольный JSON сравнивать нельзя — «строже» для него
не определено. Policy DSL (условия, выражения, предикаты) — отдельный движок,
который в v0.5 не нужен и запрещён требованиями.

## Решение

`governance` — запись с фиксированным набором ключей. Каждый ключ имеет
объявленный тип сравнения, задающий частичный порядок «строже–слабее»:

| Ключ | Тип сравнения | «Строже» означает |
|---|---|---|
| `maxAutonomyLevel` | упорядоченный enum `supervised < assisted < autonomous` | меньший индекс |
| `requireApprovalForRun` | булев, строгий = `true` | `true` |
| `requireApprovalForCompletion` | булев, строгий = `true` | `true` |
| `allowedTaskPriorities` | множество | подмножество |
| `allowedSkillProtocols` | множество | подмножество |
| `maxRunDurationSeconds` | числовой потолок, `null` = без границы | меньшее значение |
| `maxRunActions` | числовой потолок, `null` = без границы | меньшее значение |
| `maxConcurrentRuns` | числовой потолок, `null` = без границы | меньшее значение |
| `memoryScopeSharing` | упорядоченный enum `none < project < ancestors` | меньший индекс |

Ключи вне этого словаря отклоняются валидацией (`422 unknown_governance_field`)
— это и есть защита от превращения governance в свободный DSL.

Две операции определены на этой решётке:

- `stricter(a, b)` — поэлементный минимум по указанному порядку; используется для
  вычисления effective governance;
- `weakens(child, ancestor)` — возвращает список путей, по которым `child`
  слабее `ancestor`.

Проверка выполняется до commit в трёх местах: активация config revision,
создание проекта под родителем и `POST /workspaces/{id}:move`. Нарушение —
`422 governance_weakened` с `details.violations` (путь, значение предка,
значение потомка).

Перемещение Workspace перепроверяет effective governance перемещаемого проекта
**и всех затронутых потомков-проектов** в той же транзакции под tree lock. Если
хоть один потомок нарушает новую рамку, move отклоняется целиком; частично
применённых перемещений не бывает.

Effective governance вычисляется свёрткой `stricter` по слоям
`[template.default_config.governance, ancestor_effective, own_revision, profile]`.
Слои предков только ужесточают, поэтому результат монотонен вниз по дереву.
Собственный слой уже проверен на неослабление, а `stricter` дополнительно
клампит значения шаблона, который сам по себе не является явным актом этого
проекта — поэтому слабый шаблон под строгим предком молча ужесточается, а не
отклоняется.

## Последствия

- Добавление нового governance-ключа — изменение кода (словарь типов сравнения)
  и, при необходимости, миграция. Это намеренно: неизвестное поле нельзя
  сравнивать, а значит нельзя и обеспечить инвариант.
- `null` как «без границы» — самое слабое значение; проект не может заменить
  унаследованный числовой потолок на `null`.
- Governance задаётся только через config revisions (версионировано и
  аудируемо), но не через `project_profiles.settings`: попытка положить
  `governance` в `settings` — `422 governance_not_in_settings`.
- Ужесточение governance у предка не переваливает уже существующих потомков в
  «нарушение» ретроактивно на записанных ревизиях; но следующая активация или
  move у потомка будет отклонена, пока он не приведёт себя в соответствие. Это
  тот же компромисс, что и в ADR-0029.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/domain/project.py, pattern: '"unknown_governance_field"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/project.py, pattern: 'def stricter\('}
  repo: control-plane
- grep: {path: src/control_plane/domain/project.py, pattern: 'def governance_violations\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/projects.py, pattern: '"governance_weakened"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: 'await assert_subtree_governance_valid\('}
  repo: control-plane
- grep: {path: src/control_plane/domain/project.py, pattern: '"governance_not_in_settings"'}
  repo: control-plane
```
