# ADR-0060: Снимки знаний и доменные пакеты через Control Plane

Статус: Accepted (2026-09-23; правки по ревью TASK-000306 — 2026-09-23)

Контекст: амендмент 2026-09-23 к TAI-ADR-0042 суперпроекта («память — только
через Control Plane») и TAI-ADR-0031 п.6: никто, кроме ядра, не ходит в
memory-service; разрешения применяет ядро. Продолжает
[ADR-0054](0054-governed-graph-memory.md) (namespace и provenance определяет
сервер) и [ADR-0055](0055-policy-authorize-and-shadow-mode.md) (`authorize()` с
ресурсом).

## Контекст

Коннекторы знаний (первый — `integrations/selfdev` суперпроекта, формат
`integrations/selfdev/SNAPSHOT.md`) периодически снимают состояние источника
(репозиторий, трекер) и сверяют его с памятью: что появилось, что изменилось,
что исчезло. До амендмента коннектор писал в memory-service напрямую со своим
ключом и сам выбирал namespace — то есть сам решал, куда и с какой видимостью
попадает знание. Это обходит модель разрешений платформы.

Доменные пакеты (набор типов сущностей и связей, которые понимает память) и
их включение для namespace — тоже операции над памятью, которые раньше
выполнялись в обход ядра.

## Решение

### `POST /api/v1/knowledge/snapshots`

Тело — документ снимка как есть плюс `workspaceId`:

| Поле | Смысл |
|---|---|
| `workspaceId` | Workspace, от имени которого пишется знание. Обязателен. |
| `pack` | Доменный пакет снимка (1..128). Необязателен, как у памяти (`pack: str = ""`): без него память проверяет виды по каталогу namespace. |
| `source` | Источник (1..200). |
| `scope` | Область снимка внутри источника — строка (до 200), часть идентичности источника у памяти; ядром не интерпретируется. Необязателен. Это не scope namespace и не scope видимости. |
| `snapshotId` | Идентификатор снимка (1..200). |
| `observedAt` | Время снимка, ISO 8601 с часовым поясом. |
| `entities[]`, `relations[]` | Объекты; содержимое проверяет память по пакету. Вместе — не больше 20000 элементов. |

Контракт строгий (как весь `/api/v1`): лишнее поле — `400 invalid_request`. В
частности `namespace` и `scopes` клиент передать не может. Границы полей
совпадают с проверками памяти (`domain/reconcile.py::parse_snapshot`:
`MAX_SOURCE_LEN`, `MAX_SCOPE_LEN`, `MAX_SNAPSHOT_ID_LEN`, `reconcile_max_items`
= 20000), поэтому заведомо непринимаемый снимок отвергается ядром (`400`) без
вызова памяти.

1. **Авторизация** — `observations.write` на ресурс
   `ResourceRef("workspace", workspaceId)` (в режиме `local` — плоское право
   ключа). Затем workspace разрешается внутри tenant'а: чужой или неизвестный —
   `404`, архивный — `422 workspace_archived`.
2. **Куда.** Ядро вычисляет namespace `tenant:<tenant>:ws:<root>`, где `root` —
   корень дерева workspace (`workspace_ancestor_ids`, последний элемент). Имя
   совпадает с объектами `memory_namespace` `ws-<id>`, которые policy-режим уже
   отдаёт памяти как видимые (`memory_visibility`). Всё дерево делит одно
   пространство: знания подпространств связываются между собой.
3. **Кому видно.** Scope видимости — `workspace:<workspaceId>`: подпространство
   пишет в namespace корня, но со своим scope, и чтение по-прежнему фильтруется
   по workspace-scope'ам читателя.
4. **Передача.** `POST /api/memory/reconcile` identity ядра — тем же клиентом и
   credential, что Context Adapter (`CP_CONTEXT_AUTH`: сервисный аккаунт IAM или
   `CP_CONTEXT_API_KEY`), с `X-Run-Id` для трассировки. Тело — модель памяти
   `ReconcileIn`: **плоский** документ снимка плюс `namespace` и `scopes` на
   верхнем уровне:

   ```json
   {"pack": "...", "source": "...", "scope": "repo:...", "snapshotId": "...",
    "observedAt": "...", "entities": [...], "relations": [...],
    "namespace": "tenant:<t>:ws:<root>", "scopes": ["workspace:<id>"]}
   ```

   Память читает снимок как `model_dump(exclude={"namespace", "scopes"})`, так
   что `scope` верхнего уровня — строка снимка, а не scope записи. Снимок
   передаётся без `workspaceId`; `null`-поля опускаются. Таймаут —
   `CP_CONTEXT_RECONCILE_TIMEOUT_SECONDS` (60 с).
5. **Ответ.** Тело ответа памяти (счётчики, `duplicate`) возвращается клиенту
   как есть, `200`. Отказы памяти отображаются по смыслу (см. «Ошибки памяти»
   ниже): `400` (снимок невалиден) → `422 snapshot_invalid`, `409` (снимок
   старее уже применённого) → `409 snapshot_stale`. Провайдер не настроен
   (`CP_CONTEXT_PROVIDER=none`) → `503 memory_disabled`. В отличие от
   `/context` деградированного ответа нет: клиент просил запись.
6. **Журнал.** После успешной сверки — событие
   `knowledge.snapshot_reconciled` (entity — workspace): `snapshotId`, `pack`,
   `source`, `observedAt`, `workspaceId`, `rootWorkspaceId`, `namespace`,
   `entityCount`, `relationCount`, `duplicate`, `counters` (целые числа ответа
   памяти, один уровень вложенности, до 32 ключей). Содержимое сущностей и
   связей в журнал не попадает. Событие не входит в whitelist context mapping и
   в память повторно не доставляется. Отказ памяти события не порождает.
7. **Размер.** Для пути снимков лимит тела —
   `CP_KNOWLEDGE_SNAPSHOT_MAX_BODY_BYTES` (8 МиБ); остальной API сохраняет
   `CP_MAX_BODY_BYTES`. Превышение — `413 request_too_large`.

   Тело читается **до аутентификации**: лимит применяет ASGI-middleware, а
   credential проверяется в зависимости эндпоинта, когда FastAPI уже прочитал
   и разобрал JSON. Значит, анонимный клиент может заставить ядро принять и
   распарсить до 8 МиБ на запрос. Риск принят: он того же рода, что и для
   любого эндпоинта с `CP_MAX_BODY_BYTES` (1 МиБ), отличается только
   множителем; объявленный `Content-Length` сверх лимита отвергается до чтения
   тела, потоковое тело обрывается на переполнении; ограничение частоты и
   размера запросов на периметре (ingress) — штатная защита платформы. Если
   этого окажется мало, аутентификацию для этого пути можно вынести в
   middleware перед чтением тела — отдельным решением.

Ни одна транзакция БД не открыта во время вызова памяти: авторизация и
разрешение workspace — в первой, событие — во второй. Если запись события
упала после успешной сверки, повтор запроса безопасен: память ответит
`duplicate`, и событие будет записано. `Idempotency-Key` этот эндпоинт не
обрабатывает — повтор снимка идемпотентен на стороне памяти по `snapshotId`.

### Доменные пакеты

Реестр пакетов в памяти **общий для всех tenant'ов**, а ссылка на пакет без
версии разрешается в последнюю зарегистрированную версию. Если бы регистрацию
мог делать администратор tenant'а, админ tenant A выпустил бы `pack@99` и
сломал strict-проверку снимков у tenant B, включившего `pack` без версии.
Решение владельца (ревью TASK-000306):

- `POST /api/v1/knowledge/packs` — манифест пакета как есть →
  `POST /api/memory/packages` (модель памяти `PackIn`: `name`, `version` и
  остальной манифест). Только **администраторы платформы**: principal'ы из
  настройки ядра `CP_KNOWLEDGE_PACK_ADMINS` (JSON-список id principal'ов
  Control Plane или IAM; вызывающий проходит, если в списке его CP principal
  id или IAM `sub`). Пустой список (по умолчанию) — эндпоинт закрыт для всех:
  `403 permission_denied` с `details.required = "knowledge_pack_admin"`. Право
  `admin` tenant'а для этого не нужно и недостаточно. Манифест без `name`
  (непустая строка) или `version` (строка или число, не bool) — ядро отвечает
  `422 pack_invalid` с `details.field` само, не спрашивая память: иначе модель
  `PackIn` ответила бы своим `422`, а клиент увидел бы `502`. Грамматику имени
  и версии и остальную структуру проверяет память: её `400` →
  `422 pack_invalid`, `409` (версия уже зарегистрирована с другим содержимым,
  версии иммутабельны) → `409 pack_version_conflict`. Ответ памяти
  (`{status: created|unchanged, pack}`) возвращается как есть, `200`.
- `PUT /api/v1/workspaces/{id}/knowledge-packs` — `{packs: [...], strict}` →
  `PUT /api/memory/namespaces/{ns}/kinds` с телом
  `{"packages": [...], "strict": bool}` (модель памяти `NamespaceKindsIn`).
  Право — `workspaces.manage` на `workspace:<id>`. Пакеты и strict — свойство
  namespace, то есть всего дерева, поэтому `id` должен быть корнем: для
  подпространства — `422 workspace_not_root` с `details.rootWorkspaceId`
  (иначе администратор поддерева менял бы память корня). Принимаются **только
  закреплённые ссылки `name@version`** (грамматика имени и версии — как у
  памяти, `core.kinds`: `^[a-z0-9][a-z0-9._-]{0,63}@[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$`);
  ссылка без версии — `422 pack_version_required` с `details.packs`, так что
  новая версия пакета, выпущенная позже, на namespace не влияет, пока его
  администратор сам не переключит ссылку. Повторы в `packs` схлопываются.
  Память отвечает `404` на неизвестный пакет → `422 pack_not_found`, `400` →
  `422 pack_invalid`.

**Аудит.** Обе операции пишут событие в журнал ядра после успешного ответа
памяти (отказ памяти события не порождает):

- `knowledge.pack_registered` — entity `knowledge_pack` с устойчивым id
  `uuid5(NAMESPACE_URL, "knowledge-pack:<name>@<version>")` (пакет живёт в
  памяти, строки в ядре у него нет), tenant — tenant администратора; payload:
  `name`, `version` (нормализованные памятью), `status`
  (`created`/`unchanged`). Кто — `actorId` и `iamActorId` события. Манифест в
  журнал не пишется.
- `knowledge.packs_configured` — entity `workspace` (корень); payload:
  `workspaceId`, `namespace`, `packs` (закреплённые ссылки), `strict`.

### Ошибки памяти

Ядро отображает ответ памяти по смыслу и не повторяет запросы с `4xx`:

| Память | Где | Ответ ядра |
|---|---|---|
| `400` (`SnapshotError`, `PackError`) | reconcile | `422 snapshot_invalid` |
| `400` (`PackError`) | packages, kinds | `422 pack_invalid` |
| `404` (`PackNotFoundError`) | kinds | `422 pack_not_found` |
| `409` (`StaleSnapshotError`) | reconcile | `409 snapshot_stale` |
| `409` (`PackConflictError`) | packages | `409 pack_version_conflict` |
| `401`/`403`, иной `4xx` | все | `502 memory_unavailable`, `details.retryable = false` — ошибка конфигурации ядра (credential, scope сервис-аккаунта) или wire-контракта, а не клиента |
| `5xx`, транспорт, таймаут, IAM недоступен | все | `502 memory_unavailable`, `details.retryable = true` |

`details` содержит только `memoryStatus` (и `retryable` у `502`); текст ответа
памяти клиенту не отдаётся — он пишется в лог ядра (`memory call failed`).

### Клиенты

`control-plane-client`: `submit_knowledge_snapshot(workspace_id=, snapshot=)`,
`register_knowledge_pack(pack)`,
`set_workspace_knowledge_packs(workspace_id, packs=, strict=)`. MCP-инструментов
нет: это машинный путь коннектора, а не инструмент агента.

## Последствия

- Коннектор `integrations/selfdev` переходит на этот эндпоинт
  (`ControlPlaneSnapshotSink`) отдельным изменением суперпроекта; его ключ
  memory-service больше не нужен.
- Wire-формат трёх вызовов памяти (`/api/memory/reconcile`,
  `/api/memory/packages`, `/api/memory/namespaces/{ns}/kinds`) зафиксирован
  здесь и в `context_provider/http.py` и закреплён двумя способами:
  `tests/unit/test_knowledge_memory_contract.py` валидирует тела, которые
  строит ядро, по снимку запросных моделей памяти
  (`tests/fixtures/memory_knowledge_contract.json`: `ReconcileIn`, `PackIn`,
  `NamespaceKindsIn` из `app.openapi()` memory-service и поля снимка из
  `parse_snapshot`, с ревизией источника); `tests/contract` проверяет
  reconcile/kinds против живого memory-service (при `CP_TEST_MEMORY_URL`).
  Снимок фикстуры нужно обновлять вместе с изменением этих эндпоинтов памяти.
- `memory:service` — identity ядра для **всех** служебных маршрутов памяти
  (reconcile, `packages`, `namespaces/{ns}/kinds`; амендмент MEM-ADR-020).
  Отдельного токена для них нет — один токен на audience (ADR-0013): scope
  входит в `CP_CONTEXT_IAM_SCOPES` по умолчанию
  (`memory:read memory:write memory:tenants memory:service`, в режиме
  `policy` плюс `memory:on-behalf`). Потолок service account ядра содержит
  его (суперпроект 777e7ad, выложено на staging). Если потолок scope не
  выдал, память отвечает `403`, ядро — `502` с `memoryStatus: 403`.
- Кэшированный токен ядра сбрасывается только на `401` от памяти (токен
  отозван или ротирован — повтор обменяет новый). `403` — валидный токен без
  права: новый токен не поможет, а сброс общего токена на каждом отказе
  лишь нагружал бы IAM.
- Риск общего реестра (tenant-admin публикует версию, ломающую strict у
  другого tenant'а) закрыт: регистрация — только администраторы платформы,
  включение — только закреплённые версии.
- Путь `/api/v1/knowledge/*` при включённом entitlement — отдельная фича
  `knowledge` (`feature_for_path`).

## Conformance

```conformance
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"knowledge.snapshot_reconciled"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: 'Permission.OBSERVATIONS_WRITE, resource=ResourceRef\("workspace"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '"/api/memory/reconcile"'}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def submit_knowledge_snapshot"}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '\{\*\*snapshot, "namespace": namespace, "scopes": scopes\}'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: "settings.knowledge_pack_admins"}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"pack_version_required"'}
  repo: control-plane
```
