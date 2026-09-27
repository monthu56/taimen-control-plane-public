# ADR-0045: Scoped Tool Discovery View и re-authorization при исполнении

Статус: Принято (v0.7)

Предпосылка: spike HRS-3 выполнен и зелёный (`tests/unit/test_tool_discovery.py`,
`tests/integration/test_tool_discovery_v07.py`,
`tests/concurrency/test_tool_authorization.py`), измерения получены на reference
dataset. Контракт — `docs/specs/TASK-000005-scoped-tool-discovery.md`, план —
`docs/plans/TASK-000005-scoped-tool-discovery.md`, угрозы и матрица —
`docs/verification/TASK-000005-threat-model.md`.

## Контекст

Большой MCP/plugin catalog не помещается в prompt: на reference dataset из 500
инструментов eager-режим стоит ~97 000 оценочных input-токенов на задачу.
Очевидное решение — bridge вида «поиск + описание + вызов» — переносит проблему
в безопасность: если поиск отвечает по всему registry, а вызов доверяет тому,
что schema однажды попала в prompt, progressive disclosure становится обходом
permissions.

В v0.6 у Control Plane уже были registry (`skills`), назначения
(`principal_skills`), governance проекта и self-declared harness capabilities,
но не было ни bounded projection, ни ревизий для инвалидации кэша. Хуже:
`record_run_action` резолвил **любой** skill тенанта — назначение и governance
при исполнении не перепроверялись.

## Решение

### Четыре слоя вместо одного каталога

| Слой | Вопрос |
|---|---|
| Capability Catalog | что runtime технически умеет |
| Effective Tool Policy | что разрешено этому Principal/Run/workspace сейчас |
| Tool Discovery View | bounded проекция пересечения |
| Action Authorization | можно ли исполнить **прямо сейчас** |

Ключевой инвариант: **discovery не является источником права**. Между
`describe` и вызовом мир меняется, поэтому решение принимается заново, из
авторитетного состояния, в той же транзакции и под теми же locks, что и запись
действия.

### Одна функция решения

`decide_visibility` в `domain/tool_discovery.py` — единственная реализация. Ею
пользуются манифест (HRS-2), discovery и invocation. Вторая реализация рано или
поздно разошлась бы с первой, и разошлась бы молча: поиск предлагал бы то, что
gate отклоняет, или — гораздо хуже — gate принимал бы то, что поиск не имел
права показать.

### Authorization и capability — разные измерения

`visible = authorized ∧ capable`, где `authorized` — назначение, статус и
governance, а `capable` — объявленные сессией `skills.protocol.*`.
**Исполнение гейтится только authorization.** Объявленные capabilities — запись
утверждения клиента о себе (тот же принцип, что для declared sections в
ADR-0043); строить на них запрет означало бы отдать клиенту управление
собственной авторизацией. Несовпадение фиксируется в
`metadata.capabilityMismatch`, а не отклоняется.

### Ревизии выводятся, а не хранятся

`catalogRevision` — хеш `(skillId, rowVersion, status)` по каталогу тенанта;
`policyRevision` — хеш всего, что сужает каталог до Run; `viewHash` — их
комбинация с параметрами страницы, отдаётся как `ETag`. Отдельный счётчик был
бы ещё одним местом, где его можно забыть увеличить, и не покрывал бы удаление
строк. Канонизация и хеш — общие с манифестом (`domain/canonical.py`), чтобы
«одинаковое содержимое» означало одно и то же на всей платформе.

### Единый ответ на «нет» и «не для тебя»

`GET /tools/{ref}` отвечает `404 tool_not_found` и на несуществующий, и на
неразрешённый инструмент; оба случая идут одним путём резолва. Различать их
означало бы отдать enumeration oracle по именам инструментов тенанта.

### Whitelist вместо blacklist в проекции schema

Сохраняются только структурные ключевые слова JSON Schema. `default`,
`examples`, `x-*`, `$comment` удаляются: именно там в реальных MCP-манифестах
лежат endpoints, идентификаторы аккаунтов и предзаполненные токены. Whitelist
означает, что ключевое слово, о котором никто не подумал, по умолчанию
невидимо. Удалённые пути перечисляются в `schemaRedactions` — санитизация
должна быть видимой, а не молчаливой. Если secret material переживает
whitelist, схема не отдаётся вовсе (fail closed), но инструмент остаётся
видимым: иначе секрет в описании прятал бы сам инструмент от операторов.

### Отказ — метрика, а не доменное событие

Отклонённый вызов ничего не коммитит. Событие, записанное в транзакции, которая
вот-вот откатится, либо исчезнет, либо потребует второго соединения ради
не-факта. Отказ виден там же, где остальные отклонённые записи: счётчик
`tool_invocation_denied_total` (низкой кардинальности, без имён инструментов) и
server-side лог. Имя инструмента, которым управляет вероятный злоумышленник, в
durable journal не попадает.

## Последствия

- Прежнее `409 skill_unavailable` при записи action заменено на
  `403 tool_not_authorized`, и теперь отклоняется также неназначенный или
  запрещённый governance инструмент. Это изменение поведения: тенанты обязаны
  назначить skills до обновления (см. `docs/migration-v0.7.md`).
- `base.toolPolicy` манифеста получил `catalogRevision`/`policyRevision`, а
  `provenance.toolPolicy` — `revisions`. `schemaVersion` остался 1: секция
  дополнена, читатели по имени поля не ломаются.
- Ревизии считаются чтением проекции каталога тенанта на каждый запрос.
  Принято при bounded catalog; стоимость линейна и покрыта индексом
  `ix_skills_tenant_status_name`.
- Поиск лексический (AND по терминам). На reference dataset это дало task
  success 0.95 при одном false negative и ~234 токена на задачу вместо ~97 000.
  Вектор/семантика — только после измеримой потребности; базовый уровень
  зафиксирован в `docs/verification/TASK-000005-verification.md`.

## Альтернативы

- **Отдавать весь каталог и фильтровать на стороне harness.** Отклонено:
  стоимость контекста и полная зависимость безопасности от клиента.
- **Хранить revision-счётчик в таблице.** Отклонено: лишний write path,
  не покрывает удаления.
- **Гейтить исполнение по объявленным capabilities.** Отклонено: клиент
  управлял бы собственной авторизацией.
- **Кэшировать effective policy на сервере.** Отклонено: кэшированное
  authorization-решение — это устаревшее authorization-решение.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/domain/tool_discovery.py, pattern: '^def decide_visibility\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/execution.py, pattern: 'code="tool_not_authorized"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/execution.py, pattern: 'observability\.inc\("tool_invocation_denied_total"\)'}
  repo: control-plane
- grep: {path: src/control_plane/domain/tool_discovery.py, pattern: 'detail\["schemaRedactions"\]'}
  repo: control-plane
- absent: {path: "src/control_plane/**/*.py", pattern: '"skill_unavailable"'}
  repo: control-plane
```
