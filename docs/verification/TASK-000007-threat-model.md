# Threat / failure model и verification matrix — HRS-7

Спутник [SPEC](../specs/TASK-000007-child-run-handle.md) и
[PLAN](../plans/TASK-000007-child-run-handle.md).

Статус: проверено — покрытие в [verification report](TASK-000007-verification.md)

## 1. Границы доверия

```
parent harness ──HTTP──▶ Control Plane API ──▶ Postgres (source of truth)
 (не доверенный)             (авторитет)          (handles + immutable results)
        │                                                  ▲
        └── child harness ──HTTP── своим Bearer-ключом ─────┘
             (не доверенный, отдельный principal)
```

Родитель и ребёнок — два независимых недоверенных клиента. Между ними нет
доверенного канала: единственная связь — авторитетные строки в Postgres.
Handle-token — locator и proof владения ссылкой; он никогда не заменяет
аутентификацию Bearer-ключом и permission check.

## 2. Угрозы

| ID | Угроза | Митигация | Проверка |
|---|---|---|---|
| T1 | Ребёнок получает больше прав, чем родитель («privilege escalation вниз») | `granted = requested ∩ effective(parent)`; превышение → `422`, не тихое сужение; enforcement на каждой authoritative операции под дочерним Run | V4, V5 |
| T2 | Внук расширяет потолок обратно до прав своего API-ключа | `effective(parent)` для дочернего Run — это `granted` его handle, а не permissions ключа; монотонность транзитивна | V6 |
| T3 | Handle-token используется как bearer credential в обход авторизации | Token — только locator: lookup → tenant → permission проверяются заново; token без валидного ключа не даёт ничего | V7 |
| T4 | Перебор/угадывание handle по id | 32-байтный секрет в token, сравнение по digest в constant time; чужой tenant → `404`, не `403` | V7, V8 |
| T5 | Ambiguous response порождает второго ребёнка (дублирование работы и списание бюджета) | Unique `(parent_run_id, correlation_id)` в БД, а не только HTTP Idempotency-Key | V1, V2 |
| T6 | Transcript, raw prompt или hidden reasoning переносятся в ребёнка «как контекст запуска» | В launch нет поля для контекста; `summary`/`description` проходят те же key/path guard'ы, что handoff-checkpoint | V11 |
| T7 | Secrets и абсолютные локальные пути в `summary`/`data` результата | `reject_secret_material` + path guard на документе результата до хеширования | V11 |
| T8 | Переписывание результата задним числом («ребёнок сказал другое») | Отдельная append-only таблица, unique `(handle_id)`, триггер против UPDATE/DELETE | V9, V10 |
| T9 | DoS объёмным результатом | Границы `summary` ≤ 2000, `artifactRefs` ≤ 50, `guard_json_document` на `data`; превышение → `422`, объём обязан уйти в Artifact | V12 |
| T10 | Zombie parent (потерявший Claim) запускает детей | Launch проходит тот же fencing gate, что checkpoint: running Run + own principal + живой Claim | V13 |
| T11 | `detach` используется, чтобы увести исполнение из-под governance-остановки | `force_cancel` каскадирует независимо от policy; policy влияет только на кооперативный сигнал | V15 |
| T12 | Отозванный handle продолжает использоваться для запуска следующего уровня | Launch проверяет revoked/expired у handle родительского Run | V16 |
| T13 | Handle используется для чтения чужого дерева Run внутри своего tenant | Resolution требует `tasks.read`, list — привязан к конкретному parent Run | V8 |
| T14 | Утечка деталей задачи через события | Payload событий — только ids, correlationId, outcome, `resultHash`; текстовые поля не публикуются | V14 |

## 3. Failure modes

| ID | Отказ | Поведение |
|---|---|---|
| F1 | Обрыв на launch после commit, до ответа | Повтор с тем же `correlationId` → `200` и тот же handle; второго ребёнка нет |
| F2 | Restart оркестратора | Handle читается из Run Context; token не требуется, контекст не восстанавливается из transcript |
| F3 | Гонка двух launch с одним `correlationId` | Unique constraint сериализует; проигравший получает существующий handle, а не ошибку |
| F4 | `force_cancel` родителя параллельно launch | Lock order task → claim → run → handle даёт один serial order; поздний launch → `409 run_not_active` |
| F5 | Ребёнок падает, не записав output | Результат пишется с `outcome=failed`, пустым `data` и валидным hash — «нет результата» не бывает у терминального Run с handle |
| F6 | Конкурентные terminal transitions ребёнка | Unique `(handle_id)` + row lock Run: ровно одна строка результата |
| F7 | `runs.output` не влезает в границы | `422 child_result_too_large`; Run остаётся `running`, ребёнок обязан вынести объём в Artifact и повторить |
| F8 | Handle истёк, ребёнок ещё работает | Resolution → `409 child_handle_expired`; дочерний Run остаётся авторитетным и наблюдается через Task/Run API |
| F9 | Revoke уже терминального handle | Идемпотентно: строка помечена, `cancelChild` не создаёт control message для терминального Run |
| F10 | Cascade при `request_cancel` родителя, часть детей уже терминальна | Control message создаётся только активным; операция идемпотентна при повторе |
| F11 | Дочерний Run стартовал без handle (Task claim'нута напрямую) | Легальный сценарий: `child_run_id` не привязывается, handle остаётся `pending`, потолок не применяется. Ограничение зафиксировано как остаточный риск R3 |
| F12 | Доставка событий at-least-once | Состояние таблиц авторитетно; события — уведомление, не источник истины |

## 4. Verification matrix

| ID | Утверждение | Как проверяется | Уровень |
|---|---|---|---|
| V1 | Повтор launch с тем же `correlationId` не создаёт второго ребёнка | integration: два вызова, сравнение `childTaskId` и счёта строк | integration |
| V2 | Параллельные launch с одним `correlationId` дают ровно одну дочернюю Task | concurrency: N одновременных запросов | integration |
| V3 | Launch атомарен: Task, relation `spawned_by` и handle появляются вместе или не появляются вовсе | integration: инъекция сбоя после создания Task | integration |
| V4 | `grant ⊄ effective(parent)` → `422 child_grant_exceeds_parent` со списком лишних | unit + integration | оба |
| V5 | Authoritative write под дочерним Run вне потолка → `403 child_grant_exceeded` | integration | integration |
| V6 | Транзитивность потолка на дереве глубины 3 | unit (алгебра narrowing) + integration | оба |
| V7 | Token без валидного Bearer-ключа и token с испорченным секретом не разрешаются | integration | integration |
| V8 | Чужой tenant не разрешает handle ни по id, ни по token (`404`) | integration | integration |
| V9 | Строка результата immutable | integration: прямые UPDATE/DELETE в БД → ошибка | integration |
| V10 | Повторный terminal transition не перезаписывает результат | integration | integration |
| V11 | Transcript-подобные ключи, secret material и абсолютные пути в результате → `422` | unit | unit |
| V12 | Превышение границ результата → `422`, Run остаётся `running` | integration | integration |
| V13 | Zombie parent (после takeover Claim) не запускает ребёнка (`409 stale_claim`) | integration | integration |
| V14 | Событие не содержит `summary`, `data` и текста задачи | contract-проверка формы payload | integration |
| V15 | `request_cancel` каскадирует при `cascade_cooperative` и не каскадирует при `detach`; `force_cancel` каскадирует всегда | integration | integration |
| V16 | Revoked/expired handle нельзя использовать для launch следующего уровня | integration | integration |
| V17 | Reconnect после restart: Run Context отдаёт активные handle, resolution работает без token | integration | integration |
| V18 | `result_hash` воспроизводим побайтово после round-trip через JSONB | unit | unit |
| V19 | Migration roundtrip upgrade → данные → downgrade → upgrade | integration | integration |
| V20 | Run без handle терминализуется как раньше (регрессия) | integration | integration |
| V21 | Глубина дерева ограничена: превышение → `422 child_depth_exceeded` | integration | integration |

Покрытие acceptance criteria задачи: 1 → V1, V2; 2 → V7, V8, V17; 3 → V4, V5,
V6; 4 → V9, V10, V12, V18; 5 → V13, V15, V16, V19 и F-сценарии F1, F4, F8.

## 5. Остаточные риски

- **R1. Handle не привязывает исполнителя.** Дочернюю Task может claim'нуть
  любой eligible principal, а не только предполагаемый исполнитель. Потолок прав
  применяется к Run под handle, но выбор исполнителя остаётся задачей
  eligibility, не handle. Полное решение — assignee/requirements на дочерней
  Task при launch, вне scope этой задачи.
- **R2. Downgrade удаляет handle и результаты.** Дочерние Task/Run переживают
  откат, но теряют потолок и bounded result. Снижено требованием
  терминализовать или отозвать активные handle до отката (PLAN, rollback).
- **R3. Обход handle прямым claim дочерней Task** (F11): исполнение легально, но
  без потолка. Полностью закрывается только запретом claim для Task,
  порождённых handle, без handle-контекста — это отдельное решение, требующее
  подтверждения на spike.
- **R4. Сервер не может доказать, что ребёнок не получил контекст родителя вне
  Control Plane.** Handle гарантирует отсутствие переноса в *наших* контрактах;
  внепротокольный канал между двумя harness остаётся вне наблюдения и
  закрывается только на уровне execution backend (HRS-1) и sandbox (HRS-6).
