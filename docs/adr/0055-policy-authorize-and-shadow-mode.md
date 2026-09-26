# CP-0055: `authorize()` и внешний PDP — режимы local / shadow / policy

Дата: 2026-09-12. Статус: Accepted.

## Решение

Доменная авторизация Control Plane получает второй источник решения —
policy-service (TAI-ADR-0025, дизайн v0 суперпроекта). Рядом с
`require(ctx, *any_of)` появляется `authorize(ctx, *any_of, resource=...)`:
асинхронная проверка с явной ссылкой на ресурс (`platform_auth.ResourceRef`).
Все вызовы `require` в async-коде переведены на `authorize`; без `resource`
вопрос ставится на уровне tenant, а команды, которым ресурс известен
(создание задачи в воркспейсе, изменение и чтение задачи, claim, решение
approval, создание воркспейса под родителем), спрашивают про него. Единственный
sync-вызов (`_require_session_access`) остаётся на `require`.

`CP_AUTHZ_MODE` выбирает, кто решает:

| Режим | Кто решает | Что делает PDP |
|---|---|---|
| `local` (умолчание) | `require` — плоские permissions credential | не вызывается |
| `shadow` | `require` | спрашивается параллельно; расхождения считаются (`authz_shadow_divergence_total`) и пишутся в журнал с decision id; сбой PDP — счётчик, не ошибка |
| `policy` | PDP для credential с IAM-идентичностью | `require` только для legacy API key; недоступность PDP — 503 `policy_unavailable`, не allow |

В режиме `policy` списки фильтруются серверно: `visible_objects(ctx, action,
type)` → `list_objects` policy-service; `list_tasks` ограничивает выдачу
воркспейсами, где разрешено `tasks.read`, плюс задачами, которыми principal
владеет, которые ему назначены или которые он создал — зеркало правила
`task` в модели PDP. В `local` и `shadow` фильтра нет.

## Identity субъекта

Субъект решения — IAM principal id (`sub` access token), не `principals.id`.
`AuthContext` несёт `iam_principal_id` (из `iam_principal_bindings`; для
legacy ключа — `None`), Control Plane вызывает PDP своей service identity
с `on_behalf_of`. Журнал событий получает колонку `iam_actor_id` (миграция
`a9c4e2d7f1b3`), заполняемую из contextvar `current_iam_actor` на пути IAM-
аутентификации: проекция policy-service строит отношения `owner`, `holder`,
`requested_by` на том же субъекте, что и bindings.

## Граница и последствия

Транзакционные гейты (claim, lease, fencing, approval eligibility, child
ceiling) не переносятся в PDP и остаются как были. Каталог действий —
`authz/catalog.yaml`, регистрируется bootstrap суперпроекта. Переход:
`shadow` не меньше недели с нулём расхождений на tenant-level bindings, затем
`policy`; миграция плоских permissions в bindings policy-service —
`deploy/policy/migrate_bindings.py` суперпроекта. Проверки:
`tests/unit/test_authorizer.py` (три режима, any-of, деградация, legacy) и
существующая матрица интеграционных тестов без изменения поведения в `local`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/config.py, pattern: 'authz_mode: str = "local"'}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: '^async def authorize\('}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: 'authz_shadow_divergence_total'}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: '"policy_unavailable"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/lists.py, pattern: 'visible_objects\(ctx, Permission\.TASKS_READ, "workspace"\)'}
  repo: control-plane
- file: authz/catalog.yaml
  repo: control-plane
```
