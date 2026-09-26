# TASK-000005: Scoped Tool Discovery View — Verification

Date: 2026-08-12

Branch: `feat/hrs-3-scoped-tool-discovery`

Base: `main` после merge TASK-000003 (`8a7781c`), Alembic head `d4e6f8a1b2c3`

Result: PASS

## Delivered contract

- `domain/tool_discovery.py` — единственная функция решения о видимости,
  выводимые `catalogRevision`/`policyRevision`/`viewHash`, whitelist-санитайзер
  JSON Schema и bounded projection.
- `domain/canonical.py` — канонизация и `sha256:<hex>`, выделены из
  Effective Harness Manifest и переиспользованы discovery.
- `GET /tools` и `GET /tools/{ref}` — bounded search/describe с `ETag` =
  `viewHash` и `If-None-Match` → `304`.
- Re-authorization при записи run action: `403 tool_not_authorized`,
  `metadata.capabilityMismatch`, счётчик `tool_invocation_denied_total`.
- `base.toolPolicy.catalogRevision|policyRevision` и
  `provenance.toolPolicy.revisions` в манифесте Run.
- SDK `search_tools`/`describe_tool`, MCP `cp_search_tools`/`cp_describe_tool`.
- Alembic `e7c2a95d41b8`: индекс `ix_skills_tenant_status_name`.

## Current-session verification evidence

| Gate | Result |
|---|---|
| Новые тесты HRS-3 (unit + integration + concurrency + MCP) | 40 passed |
| Полный прогон: `uv run pytest` | 593 passed, 13 skipped; 23 Alembic deprecation warnings |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | 284 files already formatted |
| `uv run mypy src` | passed; 116 source files |
| `git diff --check` | passed |
| `uv run alembic heads` | single head `e7c2a95d41b8` |
| Migration roundtrip | `upgrade → downgrade d4e6f8a1b2c3 → upgrade head`: индекс исчезает и возвращается, данные не затронуты |

Тесты выполнялись на изолированном PostgreSQL 16 (порт 5437), поднятом
отдельно от общего `db-test`, чтобы не мешать параллельной сессии.

## Измерения (acceptance criterion 3)

`scripts/tool_discovery_benchmark.py`, reference dataset: 500 инструментов,
20 задач с известным правильным инструментом, одинаковая выборка для обоих
режимов. Оценка токенов — canonical bytes / 4.

| Метрика | Eager | Search |
|---|---|---|
| input tokens на задачу | 97 904 | 236 (медиана 248) |
| model round trips | 1 | 2 |
| task success | 1.0 | 0.95 |
| false-negative discovery | 0 | 1 |

Сокращение контекста — **99.76 %** ценой одного дополнительного round trip.
Единственный промах: запрос «send a message to the team channel» против
описания «Send a message to a team channel» — AND-матч по терминам требует
подстроку `the`, которой в описании нет. Это честная стоимость лексического
поиска и базовый уровень, относительно которого должен доказывать пользу любой
семантический вариант.

Eager-режим при этом требует 5 страниц discovery и describe на каждый
инструмент: полный каталог не помещается в один ответ даже без модели.

## Acceptance coverage

| Критерий задачи | Покрытие |
|---|---|
| 1. Вне policy нельзя найти/описать/вызвать | `test_unassigned_tool_is_invisible_and_indistinguishable_from_missing`, `test_governance_removes_a_tool_from_the_view`, `test_invocation_is_authorized_again_against_the_policy`, `test_revocation_between_describe_and_invoke_is_refused` |
| 2. Смена revision инвалидирует projection | `test_catalog_change_invalidates_the_cached_projection`, `test_assignment_change_invalidates_the_policy_revision`, unit-тесты ревизий |
| 3. Измерены success/tokens/round trips/false negatives | см. таблицу выше |
| 4. Нет sensitive schema/defaults/credentials | `test_sanitize_*`, `test_projection_never_carries_config_or_output_schema`, `test_describe_projects_a_sanitized_schema` |
| 5. Re-authorization и tenant isolation | `test_capability_mismatch_is_recorded_but_never_enforced`, `test_tenant_isolation`, concurrency-тесты |
| Миграция и совместимость | roundtrip выше; манифест дополняется полями при `schemaVersion` = 1 |

## Отклонения от первоначального SPEC

**Отказ во время invocation не пишется доменным событием.** SPEC предполагал
`run.tool_invocation_denied`. При реализации выяснилось, что отклонённый вызов
откатывает транзакцию: событие либо исчезло бы вместе с ней, либо потребовало
бы отдельного соединения ради записи не-факта. Отказ вынесен туда же, где уже
живут отклонённые записи — счётчик `tool_invocation_denied_total` в `/metrics`
и server-side лог. Имя инструмента, которым управляет вероятный злоумышленник,
в durable journal не попадает. SPEC, threat model и ADR отражают итоговое
решение.

**Изменение существующего контракта.** `POST /runs/{id}/actions` возвращал
`409 skill_unavailable` для disabled skill и молча принимал неназначенный.
Теперь оба случая — `403 tool_not_authorized`. Порядок обновления описан в
`docs/migration-v0.7.md`.

## Follow-ups

- Триграммный индекс (`pg_trgm`) для поиска по описаниям, если каталоги
  вырастут за пределы, на которых линейный скан ревизий остаётся дешёвым.
- Семантический поиск — только против измеренного здесь базового уровня
  false-negative discovery.
