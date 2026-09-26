# TASK-000005: Scoped Tool Discovery View — threat/failure model и verification matrix

Статус: completed

Связанные документы: `docs/specs/TASK-000005-scoped-tool-discovery.md`,
`docs/plans/TASK-000005-scoped-tool-discovery.md`.

## 1. Активы

| Актив | Почему ценен |
|---|---|
| Состав catalog (имена инструментов тенанта) | раскрывает внутреннюю инфраструктуру и поверхность атаки |
| `skills.config` | endpoints, headers, connection material |
| `input_schema` | может содержать внутренние адреса, defaults, vendor extensions |
| Effective policy | кто чем может пользоваться |
| Право исполнения | собственно действие во внешнем мире |

## 2. Модель нарушителя

- **A1. Скомпрометированный/любопытный harness** с валидным API key одного
  принципала: хочет расширить доступ через bridge.
- **A2. Принципал другого тенанта**: хочет увидеть чужой catalog.
- **A3. Prompt injection внутри задачи**: заставляет агента искать и вызывать
  инструмент вне его policy.
- **A4. Инсайдер с `org.manage`**: легитимно меняет registry; риск — тихое
  расширение видимости без следа.
- **A5. Сетевой наблюдатель логов/событий**: собирает утечки из audit.

## 3. Угрозы и контроли

| # | Угроза | Вектор | Контроль | Проверка |
|---|---|---|---|---|
| T1 | Обход permissions через bridge | A1 вызывает `record_run_action` с чужим skill | invocation re-authorization по authorization-измерению | `test_invocation_is_authorized_again_against_the_policy` |
| T2 | Enumeration каталога | A1 перебирает `GET /tools/{name}` | единый `404 tool_not_found` для «нет» и «не разрешено», общий путь резолва | `test_unassigned_tool_is_invisible_and_indistinguishable_from_missing` |
| T3 | Кросс-тенантная утечка | A2 подставляет UUID чужого skill | фильтр `tenant_id` во всех запросах, `404` | `test_tenant_isolation` |
| T4 | Утечка connection material | projection отдаёт `config` | `config` и `outputSchema` не входят в проекцию вообще | `test_projection_never_carries_config_or_output_schema` |
| T5 | Утечка секрета из schema | токен в `default`/`x-*` | whitelist-санитайзер + детектор secret material, fail closed | `test_sanitize_*`, `test_describe_projects_a_sanitized_schema` |
| T6 | Эскалация через self-declared capabilities | A1 объявляет любые `skills.protocol.*` | capability не участвует в invocation-решении | `test_capability_mismatch_is_recorded_but_never_enforced` |
| T7 | Устаревшая projection как основание для вызова | кэш harness | authorization пересчитывается при вызове; `viewHash` инвалидирует кэш | `test_revocation_between_describe_and_invoke_is_refused` |
| T8 | Тихое расширение видимости | A4 меняет registry | `catalogRevision`/`policyRevision` в манифесте Run; новая ревизия — новая версия манифеста | `test_manifest_records_the_revisions_discovery_used` |
| T9 | Утечка через audit | отказ пишет аргументы вызова или подконтрольное имя | отказ не пишет ничего durable: метрика `tool_invocation_denied_total` + лог | `test_denial_is_observable_without_inventing_a_domain_event` |
| T10 | DoS/абьюз поиска | огромные `limit`, длинные `query` | `limit` max 100, `query` max 200 символов, обязательная пагинация | `test_query_and_limit_bounds`, `test_pagination_is_bounded_and_terminates` |
| T11 | Отравление projection нестабильными данными | float/exotic типы в schema | общая канонизация отклоняет нестабильные значения | `test_sanitize_rejects_noncanonical` |

## 4. Failure model

| Сбой | Поведение | Проверка |
|---|---|---|
| Отзыв назначения между `describe` и `call` | `403 tool_not_authorized`, действие не записано | concurrency-тест |
| Изменение catalog во время пагинации | `catalogRevision` в каждом ответе меняется; cursor остаётся валиден по `(name, id)` | integration |
| Skill отключён (`disabled`) во время Run | исчезает из projection, вызов отклоняется | integration |
| Сессия без `skills.protocol.*` | capability-agnostic, видит всё разрешённое | integration |
| Run без проекта | governance пуст, ограничивают только назначения | integration |
| Пустой catalog | пустая страница и валидные ревизии, не ошибка | integration |
| Секрет в schema | `inputSchema` = `{"redacted": true}`, инструмент виден | unit |
| Restart между discovery и вызовом | состояние не кэшируется на сервере, решение пересчитывается | integration |

## 5. Verification matrix

| Требование задачи | Уровень | Тесты |
|---|---|---|
| AC1: вне policy нельзя найти/описать/вызвать | unit + integration + concurrency | T1, T2, T7 |
| AC2: смена revision инвалидирует projection | unit + integration | `test_view_hash_covers_page_identity`, ETag/`If-None-Match` |
| AC3: измерения eager vs search | benchmark | `scripts/tool_discovery_benchmark.py`, отчёт |
| AC4: нет sensitive schema/defaults/credentials | unit | T4, T5, T11 |
| AC5: re-authorization и tenant isolation | contract + security | T1, T3, T6 |
| Миграция | integration | upgrade → downgrade → upgrade |
| Совместимость манифеста | integration | T8 + существующие HRS-2 тесты |

## 6. Осознанно принятые риски

- **Лексический поиск даёт false negatives** при синонимах. Принято: вектор
  добавляется после измеримой потребности; benchmark фиксирует базовый
  уровень false-negative discovery, чтобы решение опиралось на числа.
- **`visible: false` для capability-несовпадения раскрывает факт назначения**
  инструмента принципалу. Принято: это его собственное назначение, не чужое.
- **Вывод ревизий требует чтения списка skills тенанта** на каждый запрос.
  Принято при bounded catalog; индекс `(tenant_id, status, name)` и
  проекция только `id/row_version/status` держат стоимость линейной и низкой.
