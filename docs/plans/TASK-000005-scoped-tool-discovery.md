# TASK-000005: Scoped Tool Discovery View — PLAN

Статус: completed

Ветка: `feat/hrs-3-scoped-tool-discovery`

База: `main` после merge TASK-000003 (`8a7781c`), Alembic head
`d4e6f8a1b2c3` (единственная).

## Ограничение параллельной работы

TASK-000006 (HRS-8) и TASK-000007 (HRS-7) не начаты. Эта ветка берёт
уникальную revision id от `d4e6f8a1b2c3`. Если параллельная ветка появится,
объединение потребует rebase или merge revision — multiple heads не считаются
готовым результатом (урок TASK-000008).

## Вертикальные TDD slices

### S1. Единая функция решения и ревизии

RED:

- unit: `decide_tool_visibility` даёт стабильные `authorized/capable/reason`
  для матрицы (assigned × status × governance × harness capability);
- unit: `catalog_revision` меняется при смене `row_version`/`status`/состава
  и не зависит от порядка строк;
- unit: HRS-2 `_tool_policy` через новую функцию даёт прежние `entries` и
  `visibility` (байтовая стабильность существующего контракта).

GREEN:

- `domain/tool_discovery.py`: dataclass'ы входа, решение, revisions;
- `domain/harness_manifest._tool_policy` делегирует решение туда же.

REFACTOR: убрать дублирование reason-строк, оставив один источник.

### S2. Bounded projection и санитизация

RED:

- unit: whitelist-санитайзер удаляет `default`, `examples`, `x-*`,
  `$comment` и рекурсивно чистит вложенные схемы, сообщая `schemaRedactions`;
- unit: secret material в schema → fail closed `{"redacted": true}`;
- unit: `config` и `outputSchema` не появляются ни в summary, ни в describe;
- unit: превышение глубины/размера отклоняется, а не усекается молча.

GREEN: `project_summary`, `project_detail`, `sanitize_schema` в том же
domain-модуле (чистые функции, без БД).

### S3. Discovery view: search и describe

RED:

- integration: назначенный инструмент находится по имени и по слову из
  описания; ненайденный `query` даёт пустую страницу с `view`;
- integration: неназначенный инструмент отсутствует в search и даёт `404` в
  describe — тот же ответ, что и несуществующий;
- integration: `limit`/`cursor` пагинация стабильна, `nextCursor` завершается;
- integration: eager-режим (`query` пуст) и search дают одинаковое множество;
- integration: смена `row_version` skill меняет `catalogRevision`, `viewHash`
  и делает `If-None-Match` невалидным (`200` вместо `304`);
- integration: tenant isolation — чужой skill не виден и не описывается.

GREEN:

- `application/queries/tool_policy.py`: `resolve_effective_tool_policy`,
  `search_tools`, `describe_tool`;
- `api/v1/tools.py` + регистрация в router, ETag через существующий
  `api/etag.py`;
- schemas и error codes.

REFACTOR: общий cursor-хелпер с `queries/lists.py`, без изменения контракта.

### S4. Invocation re-authorization

RED:

- integration: `record_run_action` с неназначенным skill → `403
  tool_not_authorized`, действие не записано, seq не вырос, budget цел;
- integration: governance запрещает protocol → отказ с тем же кодом;
- integration: назначенный и разрешённый → успех, `capabilityMismatch` в
  metadata при незаявленном протоколе;
- concurrency: отзыв назначения между describe и record даёт отказ;
- integration: отказ не оставляет durable следа с именем инструмента —
  наблюдаем только счётчик `tool_invocation_denied_total`.

GREEN: замена `_resolve_action_skill` на `_authorize_tool_invocation`,
использующую ту же функцию решения; метрика, лог, error code.

REFACTOR: вынести резолв ссылки, чтобы discovery и invocation резолвили
одинаково.

### S5. Manifest provenance и миграция

RED:

- integration: `base.toolPolicy` содержит `catalogRevision`/`policyRevision`,
  `provenance.toolPolicy.revisions` совпадает;
- integration: изменение catalog даёт новую версию манифеста (новый
  `baseHash`), неизменное состояние — нет;
- migration: upgrade → downgrade → upgrade на непустой базе.

GREEN: поля в `compile_manifest`; Alembic revision с индексом
`ix_skills_tenant_status_name` (`skills(tenant_id, status, name)`) для
bounded-поиска.

### S6. Измерения и evidence

- `scripts/tool_discovery_benchmark.py`: 500 инструментов, 20 задач,
  eager vs search;
- метрики: input tokens (canonical bytes / 4), round trips, task success,
  false-negative discovery;
- `docs/verification/TASK-000005-verification.md` с измеренными числами;
- обновление `docs/api.md`, `docs/harness-protocol.md`,
  `docs/architecture.md`, `docs/migration-v0.7.md`;
- ADR `0045-scoped-tool-discovery.md` — **только после** зелёного S1–S5.

## Миграция и совместимость

- Схема: один индекс, данных не трогает; downgrade его удаляет.
- API: `GET /tools`, `GET /tools/{ref}` — новые эндпоинты, additive.
- Manifest: секция `toolPolicy` дополняется полями, `schemaVersion` = 1.
- **Breaking behaviour**: `record_run_action` начинает отклонять skill,
  не назначенный принципалу. Это и есть требуемый AC1, но для существующих
  тенантов это изменение поведения. `docs/migration-v0.7.md` получает раздел
  «назначьте skills принципалам до обновления», а счётчик
  `tool_invocation_denied_total` показывает операторам, что ломается.

## Rollout / rollback

- Rollout: миграция (индекс) → деплой → проверка `GET /tools` на staging →
  наблюдение `tool_invocation_denied_total` в течение прогона.
- Rollback: откат образа; индекс можно оставить (безвреден) или снять
  downgrade'ом. Манифесты, записанные новой версией, остаются читаемыми
  старым кодом: новые поля игнорируются.
- Точка невозврата отсутствует: ни одна миграция не переписывает данные.

## Definition of done

1. S1–S6 зелёные, включая concurrency и migration roundtrip.
2. `make lint type test` без ошибок.
3. SPEC, PLAN, threat model, verification report и ADR на месте.
4. Commit/PR и verification report зарегистрированы как Control Plane
   Artifacts.
