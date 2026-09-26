# Threat / failure model и verification matrix — HRS-2

Спутник [SPEC](effective-harness-manifest-spec.md) и
[PLAN](effective-harness-manifest-plan.md).

## 1. Границы доверия

```
клиент harness ──HTTP──▶ Control Plane API ──▶ Postgres (source of truth)
   (не доверенный)          (авторитет)            (immutable evidence)
```

Всё, что приходит от harness, — **заявление**, а не факт. Authorization и
server-authoritative секции считаются только из БД в транзакции компиляции.

## 2. Угрозы

| ID | Угроза | Митигация | Проверка |
|---|---|---|---|
| T1 | Клиент подделывает `identity`/`projectPolicy`/`budgets`, чтобы манифест «доказал» несуществующие полномочия | Схема запроса не принимает эти секции; попытка → `422 server_authoritative_section` | V7 |
| T2 | Secrets/токены попадают в evidence через `model.params` или ephemeral `data` | `reject_secret_material` на всех harness-declared payload'ах | V8 |
| T3 | Transcript, raw prompt, chain-of-thought записываются в манифест «для отладки» | Отдельный key-guard, тот же список, что у handoff-checkpoint (`runs._SENSITIVE_HANDOFF_KEYS`) | V9 |
| T4 | Абсолютные локальные пути утекают в durable state | Path guard, повторно используемый из handoff | V9 |
| T5 | Cross-tenant чтение манифеста по угаданному `runId` | Все запросы scoped по `tenant_id`; чужой id → 404, не 403 | V10 |
| T6 | Переписывание истории: правка `base` задним числом | Триггер БД отклоняет UPDATE и DELETE | V4 |
| T7 | Тихий provider fallback, замаскированный под обычный recompile | `reason=provider_fallback` обязателен и требует роста `model.attempt`; `attempt` входит в hash | V6 |
| T8 | Ephemeral steering выдаётся за durable intent | Ephemeral — отдельная таблица и отдельное поле ответа, не входит в хеши | V5 |
| T9 | Zombie run (потерявший claim) пишет манифест | Compile проходит тот же fencing gate, что checkpoint: running + own principal + живой claim | V11 |
| T10 | DoS через гигантский declared payload | `guard_json_document`: лимиты байт, глубины, числа узлов | V8 |
| T11 | Hash-коллизия/подмена алгоритма | Алгоритм в самом значении (`sha256:…`); смена алгоритма — новая схема, не тихая подмена | V1 |

## 3. Failure modes

| ID | Отказ | Поведение |
|---|---|---|
| F1 | Memory Service недоступен на момент компиляции | `captured.memory = null`; компиляция успешна. Memory никогда не блокирует evidence |
| F2 | У Task нет проекта (workspace вне проектной иерархии) | `projectPolicy.projectId = null`, `governance = {}`, provenance `source: "absent"` |
| F3 | Гонка двух compile для одного Run | Row lock Run сериализует; проигравший видит уже созданную версию и, при равном hash, получает её же (200) |
| F4 | Повтор HTTP-запроса при неоднозначном ответе | Idempotency write flow возвращает сохранённый ответ, новая версия не создаётся |
| F5 | Компиляция падает при `start_run` | Транзакция откатывается целиком: Run не стартует. Run без evidence не создаётся |
| F6 | Старый Run (до миграции) без манифеста | `404 manifest_not_found` — предсказуемо, не 500 |
| F7 | Изменение конфигурации проекта во время Run | Активный манифест не меняется; расхождение обнаруживается при следующем compile ростом `baseHash` |

## 4. Verification matrix

| ID | Утверждение | Как проверяется | Уровень |
|---|---|---|---|
| V1 | Одни и те же входы → один и тот же `baseHash`; порядок ключей на входе не влияет | unit: сборка из перемешанных dict, сравнение байт и hash | unit |
| V2 | Изменение любой effective revision (governance, шаблон, tool policy, budgets, model) даёт новый `baseHash` | unit: таблица мутаций, каждая меняет hash | unit |
| V3 | `captured` (курсор, memory) не влияет на `baseHash` | unit: два вызова с разным курсором, hash совпал | unit |
| V4 | Строка манифеста immutable | integration: прямой UPDATE/DELETE в БД → ошибка | integration |
| V5 | Ephemeral запись не меняет `base`, `provenance` и хеши | integration: сравнение до/после | integration |
| V6 | Provider fallback создаёт новую версию с новым `modelAttempt`; повтор без роста attempt → 422 | integration | integration |
| V7 | Server-authoritative секция в теле запроса → 422 | integration | integration |
| V8 | Secret-подобный ключ и слишком большой payload → 422 | unit + integration | оба |
| V9 | Transcript/абсолютный путь в declared payload → 422 | unit | unit |
| V10 | Чужой tenant не читает манифест (404) | integration | integration |
| V11 | Zombie run (после takeover claim) не компилирует манифест (409 stale_claim) | integration | integration |
| V12 | Recompile без изменений не создаёт версию (200, та же version) | integration | integration |
| V13 | Migration roundtrip upgrade → данные → downgrade → upgrade | integration | integration |
| V14 | API-ответ не содержит credentials и prompt-подобных полей | contract-проверка формы ответа | integration |
| V15 | Run без манифеста (до миграции) → 404, не 500 | integration | integration |

## 5. Остаточные риски

- **Downgrade удаляет evidence.** Технически неустранимо без внешнего хранилища;
  снижено требованием выгрузки перед downgrade (PLAN, rollback).
- **`harness_declared` секции недоказуемы.** Сервер фиксирует заявление, но не
  может подтвердить, что harness действительно использовал заявленную модель.
  Полное решение требует attestation со стороны execution backend (HRS-1) и
  вынесено за рамки HRS-2. Provenance честно называет источник, чтобы это
  ограничение было видно аудитору, а не подразумевалось.
- **Tool visibility объясняется, но не вычисляется заново.** Манифест использует
  тот же резолвер, что и run context; расхождение правил discovery — предмет
  HRS-3.
