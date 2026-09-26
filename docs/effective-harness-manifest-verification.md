# Verification report: Effective Harness Manifest (HRS-2)

Дата: 2026-08-12. Ревизия схемы: `9c41ee0d7b52` (v0.7).
Проверяемые документы: [SPEC](effective-harness-manifest-spec.md),
[PLAN](effective-harness-manifest-plan.md),
[threat/verification matrix](effective-harness-manifest-threat-model.md),
[ADR-0043](adr/0043-effective-harness-manifest.md).

## Как проверялось

```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

PostgreSQL — реальный (`docker compose --profile test`), отдельная база
`control_plane_test_hrs2`: в общей `control_plane_test` в этот момент стояла
чужая alembic-ревизия параллельного harness (см. «Замечания»).

## Результат

| Проверка | Итог |
|---|---|
| `ruff check` + `ruff format --check` | passed |
| `mypy src` (111 файлов) | passed |
| `pytest` (весь набор) | **537 passed, 13 skipped**, 0 failed |
| из них новых | 48 unit (`tests/unit/test_harness_manifest.py`), 20 integration (`tests/integration/test_harness_manifest_v07.py`), 2 migration (`tests/integration/test_migration_v07.py`) |

## Acceptance criteria задачи

| # | Критерий | Статус | Доказательство |
|---|---|---|---|
| 1 | Одинаковые revisions → byte-stable representation и одинаковый hash | выполнен | `test_same_inputs_give_byte_stable_document_and_hash`, `test_key_order_does_not_change_canonical_bytes`, `test_tool_input_order_does_not_change_the_hash` |
| 2 | Изменение любой effective revision → новый manifest/version | выполнен | `test_any_effective_revision_change_produces_a_new_hash` (7 кейсов), `test_declaring_a_backend_creates_a_new_version`, `test_project_governance_change_shows_up_as_a_new_base_hash` |
| 3 | Operational и Memory разделены; operational авторитетен | выполнен | `test_memory_reference_is_separate_and_outside_the_base_hash`, `test_operational_and_memory_are_separate_and_unhashed` |
| 4 | Ephemeral не меняет frozen base незаметно | выполнен | `test_ephemeral_marker_never_touches_the_frozen_base` (base, baseHash и snapshotHash не изменились) |
| 5 | API не раскрывает secrets и sensitive payloads | выполнен | `test_secret_material_is_rejected`, `test_transcript_like_field_is_rejected`, `test_absolute_local_path_is_rejected`, `test_ephemeral_payload_is_guarded`, `test_manifest_compilation_emits_events` (в payload события только ссылки и хеши) |
| 6 | Migration roundtrip, contract, tenant isolation, reproducibility tests | выполнен | `test_manifest_migration_roundtrip`, `test_immutability_trigger_survives_the_roundtrip`, `test_manifest_is_tenant_scoped`, `test_server_authoritative_sections_are_rejected` |

## Verification matrix

| ID | Утверждение | Статус | Тест |
|---|---|---|---|
| V1 | Детерминизм канонического представления | passed | `test_same_inputs_give_byte_stable_document_and_hash` |
| V2 | Любая смена effective revision меняет hash | passed | `test_any_effective_revision_change_produces_a_new_hash` |
| V3 | `captured` не влияет на `baseHash` | passed | `test_captured_state_never_enters_the_base_hash` |
| V4 | Строки манифеста immutable | passed | `test_manifest_rows_are_immutable` (UPDATE и DELETE → ошибка триггера) |
| V5 | Ephemeral не меняет base и хеши | passed | `test_ephemeral_marker_never_touches_the_frozen_base` |
| V6 | Provider fallback = новая attempt; без роста attempt → 422 | passed | `test_provider_fallback_is_a_new_recorded_attempt` |
| V7 | Server-authoritative секция в теле → 422 | passed | `test_server_authoritative_sections_are_rejected` (5 секций) |
| V8 | Secret и oversized payload → 422 | passed | `test_secret_material_is_rejected`, `test_oversized_declaration_is_rejected` |
| V9 | Transcript/абсолютный путь → 422 | passed | `test_transcript_like_field_is_rejected`, `test_absolute_local_path_is_rejected` |
| V10 | Tenant isolation (404) | passed | `test_manifest_is_tenant_scoped` |
| V11 | Zombie run не компилирует | passed | `test_compile_requires_a_live_claim` |
| V12 | Recompile без изменений не создаёт версию | passed | `test_recompile_without_change_does_not_create_a_version` |
| V13 | Migration roundtrip | passed | `test_manifest_migration_roundtrip` |
| V14 | Ответ и события без секретов и содержимого | passed | `test_manifest_compilation_emits_events` |
| V15 | Run без манифеста → 404, не 500 | passed | `test_run_without_manifest_reads_as_404` |

## Что изменилось в коде

| Файл | Роль |
|---|---|
| `src/control_plane/domain/harness_manifest.py` | канонизация, hash, сборка манифеста, guard'ы деклараций (чистый домен) |
| `src/control_plane/domain/redaction.py` | единый список запрещённого в durable payload; handoff переведён на него |
| `src/control_plane/application/commands/manifests.py` | компиляция, версионирование по hash, fallback-правило, ephemeral |
| `src/control_plane/application/commands/runs.py` | auto-compile версии 1 в транзакции `start_run` |
| `src/control_plane/application/queries/execution.py` | чтение манифеста и истории версий |
| `src/control_plane/api/v1/runs.py`, `schemas.py` | четыре endpoint'а и контракт запроса |
| `src/control_plane_client/client.py`, `src/control_plane_mcp/server.py` | SDK-метод и read-only MCP-проекция `cp_harness_manifest` |
| `migrations/versions/9c41ee0d7b52_*.py` | две append-only таблицы + immutability trigger |

## Замечания и остаточные риски

1. **Downgrade удаляет evidence.** Подтверждено тестом: после
   `downgrade → upgrade` таблица пуста. Требование выгрузки перед downgrade
   зафиксировано в docstring миграции, PLAN и ADR.
2. **`harness_declared` недоказуемы.** Сервер фиксирует заявление о модели и
   backend, но не может подтвердить его без attestation (HRS-1). Provenance
   называет источник явно, чтобы ограничение было видно.
3. **Tool visibility объясняется, но не переопределяется.** Манифест добавляет
   governance-измерение (`allowedByGovernance`) к резолверу run context, не
   меняя его правил; унификация discovery — HRS-3.
4. **Общая тестовая база между harness'ами.** Контейнер `db-test` разделяется с
   параллельной сессией (worktree `control-plane-task-000003`), и её alembic
   ревизия ломала прогон. Прогон выполнен в отдельной базе; постоянное решение —
   отдельная база на harness или отдельный контейнер.
5. **Constraint на `char_length(summary)`** дублирует pydantic-валидацию
   намеренно: API — не единственный писатель, а таблица должна быть корректной
   независимо от того, кто в неё пишет.
