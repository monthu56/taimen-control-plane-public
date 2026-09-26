# ADR-0053: IAM как источник identity; bindings управляются API

Статус: Accepted (2026-09-12). Заменяет ADR-0006 в части того, что считается
credential по умолчанию.

Контекст: ADR-0006 — API-ключи `cp_<prefix>_<secret>` как механизм
аутентификации MVP; IAM-7 — Policy Enforcement Point поверх `platform-auth-sdk`
и таблица `iam_principal_bindings` (миграция `f5b91c3e7a24`); ADR-0040 —
закрытие compatibility window; ADR-0030 суперпроекта (раздел 1, пункт 2) —
«API управления `iam_principal_bindings` вместо SQL».

## Контекст

С IAM-7 Control Plane проверяет токены `iam-service` и берёт права
federated identity из `iam_principal_bindings`. Но у самой таблицы не было
управляющей поверхности: ни API, ни CLI. Каждый binding на стенде заводился
руками — `deploy/staging/core/binding.sql`, функция `ensure_binding` в
`deploy/bootstrap.py`, — и это оставляло три дефекта.

1. **Chicken-and-egg IAM-only стенда.** Bootstrap выпускает только API-ключ.
   Когда `CP_LEGACY_API_KEYS_ENABLED=false`, первый администратор не может
   войти, пока кто-то не вставит строку в базу — то есть без SSH к стенду
   IAM-only режим недостижим.
2. **Неразличимость в журнале.** Строка, вставленная SQL, не оставляет события:
   в `events` нет ни того, кто дал права, ни каких. У API-ключей это есть
   (`api_key.created` / `api_key.revoked`), у federated identity — нет.
3. **Залипание кэша.** Binding, появившийся после первого запроса identity,
   виден enforcement только по истечении `stale_after` отрицательного ответа;
   отозванный — по истечении TTL положительного. SQL не может сообщить
   процессу, что строка изменилась.

Одновременно окно совместимости фактически закрыто: staging переведён на
IAM-only, а API-ключ остаётся только аварийным входом при недоступности IAM.
ADR-0006 всё ещё описывает ключ как *механизм аутентификации* — это уже не так.

## Решение

### 1. IAM — источник identity; API-ключ — bootstrap и аварийный путь

Credential по умолчанию для людей, агентов и сервисов — токен IAM. Права по
нему даёт binding, потолок — scope токена (IAM-7). API-ключ `cp_` остаётся:

- credential, который выдаёт bootstrap — единственный способ сделать первый
  административный вызов, если binding при bootstrap не задан;
- аварийным входом, если IAM недоступен (`CP_LEGACY_API_KEYS_ENABLED=true`
  включается осознанно и на время инцидента).

Новые интеграции на ключах не строятся. Решение ADR-0006 о формате и
хранении ключа остаётся в силе; его статус — Superseded в части «ключ как
способ аутентификации».

### 2. Bootstrap создаёт первый binding

`POST /api/v1/bootstrap` принимает необязательное поле
`iamBinding: {issuer, iamTenantId, iamPrincipalId}`. При наличии в той же
транзакции, что tenant, admin principal и ключ, создаётся строка
`iam_principal_bindings` для admin principal. Права — **все permissions по
имени**, а не только `admin`: scope первого токена работает потолком, и PAT,
выпущенный на `read+write`, сузил бы голый `admin` до пустого множества.
Ответ получает `iamBinding` (полная проекция binding, `null` без запроса).

### 3. Bindings управляются API

| Маршрут | Право | Семантика |
|---|---|---|
| `GET /api/v1/principals/{id}/iam-bindings` | `principals.read` | все identity principal'а, включая отозванные |
| `POST /api/v1/principals/{id}/iam-bindings` | `principals.write` | upsert по паре `(issuer, iamPrincipalId)`: нет строки — `201`, есть — `200`, строка перенаправляется на principal, права заменяются, статус возвращается в `active` |
| `POST /api/v1/iam-bindings/{id}:revoke` | `principals.write` | `status=revoked`, `revoked_at=now()`; идемпотентно |

Правила у upsert те же, что у выпуска API-ключа, с теми же кодами:

- неизвестное право — `422 invalid_permissions`;
- не-admin не может дать `admin` — `422 invalid_permissions`;
- не-admin не может дать права, которых не держит сам — `403
  permission_escalation` с `details.missing`;
- principal должен быть `active` — `422 principal_not_active`.

Добавляется правило о **виде** principal: `agent` и `service` не получают
`admin` и `approvals.decide` — `422 permissions_not_allowed_for_kind` с
`details.forbidden`. Агент с `admin` переписал бы собственный binding; агент с
`approvals.decide` утверждал бы гейт, поставленный, чтобы его остановить.

Пара `(issuer, iamPrincipalId)` уникальна между tenant'ами по схеме; попытка
перенаправить чужую строку — `409 iam_identity_bound_elsewhere`.

Каждая операция пишет событие в журнал: `iam_binding.created`,
`iam_binding.updated`, `iam_binding.revoked` с `principalId`, `issuer`,
`iamPrincipalId` и (для created/updated) `permissions`. Секретов у binding
нет, поэтому редактировать в payload нечего.

### 4. Статус `revoked` и кэш

CHECK-ограничение статуса расширяется до `active | disabled | revoked`
(миграция `c2d8e4f6a1b3`): `disabled` остаётся операторским переключателем,
`revoked` — сознательным отсечением через API. Enforcement трактует оба
одинаково (любой не-`active` закрывает вход), а upsert той же identity
открывает строку заново.

После коммита операции роутер вызывает `BindingDirectory.invalidate(issuer,
iamPrincipalId)`: сбрасывается снимок identity и все записи revocation-кэша
credential'ов, которые по этой identity отвечали. Изменение binding действует
со следующего запроса *этого процесса* — включая отрицательный ответ,
закэшированный до появления строки (пункт 3 ADR-0030 суперпроекта). Между
процессами кэш не согласуется: это осознанная граница, и на multi-instance
она закрывается TTL, как и прежде.

### 5. SDK

`ControlPlaneClient` получает `list_iam_bindings(principal_ref)`,
`upsert_iam_binding(principal_ref, *, issuer, iam_tenant_id, iam_principal_id,
permissions)` и `revoke_iam_binding(binding_id)`; записи идут с
`Idempotency-Key`. MCP-инструмента нет намеренно: управление identity — задача
оператора, а не агента внутри run.

## Альтернативы

- **Оставить SQL, добавить только скрипт.** Не решает ни chicken-and-egg (скрипту
  всё равно нужен доступ к базе), ни журнал, ни кэш.
- **Право `admin` на все три маршрута.** Тогда проверка «подмножество прав
  вызывающего» становится мёртвым кодом: admin держит всё. Выбрана та же
  пара прав, что у API-ключей, — binding и есть их IAM-аналог; ужесточение до
  `admin` — одна строка `require(...)`, если понадобится.
- **Хранить права в токене IAM.** Отклонено ещё в IAM-7: право «создать Task»
  принадлежит продукту, а не identity provider.
- **Отдельная таблица revocations вместо статуса.** Избыточно: `revoked_at`
  уже есть, нужен был только различимый статус.

## Последствия

- IAM-only стенд разворачивается без SSH-доступа к базе: bootstrap с
  `iamBinding`, дальше — API от имени администратора.
- `deploy/bootstrap.py` (`ensure_binding`) и `binding.sql` переводятся на API
  отдельной задачей суперпроекта; до этого они продолжают работать — таблица не
  менялась, кроме CHECK.
- ADR-0006 помечен Superseded; формат ключа и его хранение не пересматриваются.
- Конформанс — `tests/integration/test_iam_bindings.py`; форма контракта —
  `tests/unit/test_iam_binding_schemas.py`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- migration: c2d8e4f6a1b3
  repo: control-plane
- route: "POST /principals/{principal_id}/iam-bindings"
  repo: control-plane
- route: "POST /iam-bindings/{binding_id}:revoke"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/iam_bindings.py, pattern: '"permissions_not_allowed_for_kind"'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/principals.py, pattern: 'bindings\.invalidate\(issuer, iam_principal_id\)'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/schemas.py, pattern: 'iam_binding: IamIdentitySpec \| None'}
  repo: control-plane
```
