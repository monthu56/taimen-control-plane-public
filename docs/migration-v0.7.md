# Миграция v0.6 → v0.7: Durable Active Turn Control и Child Run Handle

Revision TASK-000003: `c91f3c7ad8e2` (revises `72ef8bc31a06`).

`d4e6f8a1b2c3` объединила Active Turn Control `c91f3c7ad8e2` и Effective
Harness Manifest `9c41ee0d7b52`; поверх неё легла Scoped Tool Discovery
`e7c2a95d41b8`.

Текущий head: `a7f2c4d19b60` (TASK-000007, Durable Child Run Handle).

## Что добавлено

- таблица `run_control_messages` с Run-local ordering, domain idempotency,
  lifecycle и safe-boundary acknowledgement;
- HTTP/SDK/MCP create/list/ack contract;
- capability `active_turn_control.v1`;
- pending control messages в Run Context;
- compatibility materialization старого `:request-cancel`;
- `force_cancel` с release Claim и cascade по `spawned_by` descendants.

## Что добавил Durable Child Run Handle (TASK-000007)

- таблицы `run_child_handles` и `run_child_results`; вторая append-only с
  триггером immutability;
- launch/list/resolve/revoke endpoint'ы, SDK и MCP tools;
- capability `child_run_handle.v1`;
- `childHandles` в Run Context — путь reconnect после restart;
- потолок прав дочернего Run и его проверка на authoritative writes;
- третье измерение `allowedByChildGrant` в `toolPolicy` манифеста.

**Порядок деплоя важен:** сервер обращается к `run_child_handles` при старте
любого Run, поэтому миграция накатывается до нового кода. Обратный порядок даёт
`UndefinedTable` на `:start-run`.

## Совместимость

Изменение additive. Старые harness'ы продолжают использовать
`:request-cancel`; сервер создаёт соответствующее typed message. Новые tools
следует включать после деплоя миграции и обновления harness.

Параллельные revisions объединены явной merge revision; release state
имеет одну Alembic head.

## Scoped Tool Discovery (TASK-000005, revision `e7c2a95d41b8`)

Добавлено:

- `GET /tools` и `GET /tools/{ref}` — bounded projection пересечения
  Capability Catalog и Effective Tool Policy, с `catalogRevision`,
  `policyRevision` и `viewHash` (ETag);
- SDK `search_tools`/`describe_tool` и MCP `cp_search_tools`/`cp_describe_tool`;
- `base.toolPolicy.catalogRevision|policyRevision` и
  `provenance.toolPolicy.revisions` в Effective Harness Manifest;
- индекс `ix_skills_tenant_status_name`; данные не переписываются.

### Изменение поведения, требующее действия до обновления

`POST /runs/{id}/actions` теперь **пересчитывает effective tool policy** и
отклоняет `skill`, который не назначен принципалу, отключён в registry или
запрещён governance проекта: `403 tool_not_authorized` вместо прежнего
`409 skill_unavailable` (и вместо прежнего молчаливого успеха для назначенных
не по policy инструментов).

Порядок действий:

1. до деплоя назначить принципалам skills, которые они реально используют
   (`POST /principals/{id}/skills`);
2. сверить `governance.allowedSkillProtocols` проектов с протоколами этих
   skills;
3. после деплоя наблюдать счётчик `tool_invocation_denied_total` в `/metrics` —
   ненулевой рост означает пропущенное назначение.

Отказ ничего не коммитит: seq действий не растёт, бюджет не расходуется,
доменное событие не пишется (сам отказ виден в метрике и server-side логе).

## Rollback

Downgrade до `e7c2a95d41b8` удаляет `run_child_handles` и `run_child_results`:
дочерние Task, Run и relations `spawned_by` переживают откат, но теряют потолок
прав и bounded results. Перед откатом оператор обязан терминализовать или
отозвать живые handle.

Downgrade до `d4e6f8a1b2c3` снимает индекс `ix_skills_tenant_status_name`;
манифесты, записанные новой версией, читаются старым кодом — лишние поля
секции `toolPolicy` игнорируются. Downgrade до `72ef8bc31a06` удаляет
`run_control_messages`. Перед rollback
оператор должен остановить новые control writes и убедиться, что нет
неприменённых сообщений. Append-only events остаются историей; старый сервер
их игнорирует как неизвестные event types.
