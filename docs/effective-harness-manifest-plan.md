# PLAN: Effective Harness Manifest for Run (HRS-2)

Спутник [SPEC](effective-harness-manifest-spec.md). План вертикальный: каждый
шаг оставляет систему в рабочем состоянии и проверяем сам по себе.

## Шаг 0. Spike (без БД и HTTP)

Чистый доменный модуль `domain/harness_manifest.py`:

- `canonical_bytes` и `manifest_hash`;
- `build_manifest(inputs) -> CompiledManifest(base, provenance, captured)`;
- guard'ы для harness-declared секций.

Проверяется unit-тестами без Postgres. Критерий успеха spike (из
`hermes-agent.md`, HRS-2): повторная компиляция из тех же revisions даёт тот же
hash; манифест объясняет происхождение каждого tool и policy; operational и
memory не смешиваются; ephemeral не меняет base; provider fallback даёт новую
recorded attempt.

**Гейт: ADR пишется только после того, как этот шаг зелёный.**

## Шаг 1. Миграция

Одна ревизия Alembic поверх `72ef8bc31a06`:

- `run_harness_manifests` и `run_manifest_ephemerals` со всеми CHECK/UNIQUE;
- триггер immutability на обе таблицы (`BEFORE UPDATE OR DELETE` → RAISE);
- индексы: `(tenant_id, created_at, id)`, `(run_id, version DESC)`,
  `(manifest_id, seq)`.

`downgrade()` симметрично удаляет таблицы и триггерные функции. Миграция
аддитивна: ни одна существующая таблица не меняется, поэтому старый код
продолжает работать на новой схеме (forward compatibility для rolling deploy).

## Шаг 2. Компиляция при старте Run

`start_run` внутри своей транзакции собирает входы и пишет манифест `version=1`
с `reason=run_started`. Отказ компиляции — отказ старта Run: манифест не
опционален, иначе появляется класс Run без evidence.

Событие `run.manifest_compiled` пишется в той же транзакции.

## Шаг 3. Recompile и provider fallback

Команда `compile_manifest(run_id, declared, reason)`:

1. lock Run (порядок блокировок как везде: task → claim → run);
2. проверка running + принадлежность principal + fencing gate;
3. сбор server-authoritative входов заново;
4. `baseHash` считается и сравнивается с активной версией;
5. равен → возврат активной версии, статус 200, событие не пишется;
6. отличается → `version + 1`, `supersedes_version = active.version`, событие.

`reason=provider_fallback` требует `model.attempt > активного` — иначе `422
invalid_fallback_attempt`. Это защищает от «тихого» fallback, который выглядит
как обычный recompile.

## Шаг 4. Ephemeral

`record_ephemeral(run_id, kind, summary, data)` — append-only строка к активной
версии, `seq` под row lock манифеста. Валидация: bounded размер, secret guard,
запрет абсолютных путей.

## Шаг 5. API и MCP

- четыре endpoint'а из SPEC §8 в `api/v1/runs.py`, схемы в `api/v1/schemas.py`;
- compile и ephemeral идут через `execute_write` (idempotency);
- read-only MCP tool `cp_harness_manifest`.

## Шаг 6. Документация

ADR-0043 (после зелёного spike), обновление `docs/api.md`,
`docs/architecture.md`, `docs/harness-protocol.md`, ссылка из roadmap
верхнеуровневого репозитория.

## Тесты

| Уровень | Файл | Что проверяет |
|---|---|---|
| unit | `tests/unit/test_harness_manifest.py` | канонизация, hash, детерминизм, отказ float, guard'ы, build_manifest |
| integration | `tests/integration/test_harness_manifest.py` | auto-compile при старте, recompile без изменений (200), с изменениями (201), fallback, ephemeral, immutability, 404 для чужого tenant |
| integration | `tests/integration/test_migration_v07.py` | upgrade → данные → downgrade → upgrade (roundtrip) |
| contract | в integration-наборе | форма ответа API, отсутствие secrets, отказ server-authoritative секций |

## Compatibility

- Аддитивная миграция; старые Run без манифеста читаются с `404
  manifest_not_found`.
- Клиенты, не знающие про манифест, не ломаются: ни один существующий ответ не
  меняет форму. `start_run` возвращает тот же `RunOut`.
- Формат документа версионируется полем `schemaVersion` внутри `base`.

## Rollout

1. Миграция (аддитивна, безопасна на живой базе; таблицы пустые).
2. Деплой кода: auto-compile начинает работать для новых Run.
3. Наблюдение: доля Run с манифестом должна стать 100% для стартовавших после
   деплоя; расхождение — сигнал об ошибке компиляции, а не о норме.

Feature flag не вводится намеренно: манифест — evidence, а частично включённое
evidence хуже отсутствующего (появляется молчаливая дыра в аудите).

## Rollback

- Код: откат релиза безопасен — новые таблицы просто перестают наполняться,
  существующие строки остаются.
- Схема: `alembic downgrade` удаляет таблицы, то есть **удаляет evidence**.
  Поэтому downgrade в production выполняется только после выгрузки
  `run_harness_manifests` и `run_manifest_ephemerals` в артефакт-хранилище.
  Это записано и в runbook, и в docstring миграции.
