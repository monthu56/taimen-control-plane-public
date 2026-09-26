# Verification report: Durable Child Run Handle (HRS-7)

Дата: 2026-08-13. Ревизия схемы: `a7f2c4d19b60` (v0.7).

Проверяемые документы: [SPEC](../specs/TASK-000007-child-run-handle.md),
[PLAN](../plans/TASK-000007-child-run-handle.md),
[threat/verification matrix](TASK-000007-threat-model.md),
[ADR-0046](../adr/0046-durable-child-run-handle.md).

## Как проверялось

```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```

PostgreSQL — реальный (`docker compose --profile test up -d db-test`), общая
тестовая база `control_plane_test`, миграции накатываются Alembic'ом один раз
за сессию.

## Результат

| Проверка | Итог |
|---|---|
| `ruff check` + `ruff format --check` | passed |
| `mypy src` (117 файлов) | passed |
| `pytest` (весь набор) | **610 passed, 13 skipped**, 0 failed |
| из них новых | 25 unit (`tests/unit/test_child_handle.py`), 30 integration (`tests/integration/test_child_run_handle.py`), 2 concurrency (`tests/concurrency/test_child_run_handle.py`) |

## Acceptance criteria задачи

| # | Критерий | Статус | Доказательство |
|---|---|---|---|
| 1 | Повтор launch с correlation id не создаёт второго child execution | выполнен | `test_repeat_with_the_same_correlation_id_returns_the_same_child`, `test_parallel_launches_with_one_correlation_id_create_one_child` (6 параллельных запросов с **разными** Idempotency-Key → ровно один `201`, один child, один token) |
| 2 | Handle переживает restart и разрешается только в правильном tenant/permission scope | выполнен | `test_run_context_carries_child_handles_for_reconnect` (reconnect без token и без transcript), `test_resolve_by_id_and_by_token`, `test_token_alone_grants_nothing_without_a_key` (401), `test_foreign_tenant_cannot_resolve_a_handle_by_token` (404), `test_child_handles_are_scoped_to_their_tenant` |
| 3 | Дочерний Run не расширяет permissions родителя | выполнен | `test_grant_beyond_the_parent_ceiling_is_rejected` (422 + список лишних), `test_child_cannot_write_beyond_its_ceiling` (ключ ребёнка **сильнее** его потолка → 403), `test_grandchild_cannot_recover_what_its_parent_gave_up`, `test_ceiling_is_monotone_across_three_levels`, `test_child_sees_and_uses_only_the_skills_its_handle_granted` |
| 4 | Terminal result immutable, bounded и проверяется hash | выполнен | `test_child_success_records_a_hashed_bounded_result`, `test_a_recorded_result_cannot_be_rewritten` (триггер БД + повторная попытка не переписывает), `test_oversized_child_output_is_rejected_and_the_run_keeps_running`, `test_result_hash_is_stable_and_independent_of_key_order`, `test_result_document_survives_a_json_round_trip_byte_for_byte` |
| 5 | Покрыты cancel/reconnect, stale claim, expiry/revocation и ambiguous-response tests | выполнен | `test_cooperative_cancel_follows_policy`, `test_force_cancel_cascades_regardless_of_policy`, `test_launch_requires_the_parent_run_holder_and_a_live_claim` (409 `stale_claim`), `test_revoke_is_idempotent_and_blocks_further_launches`, `test_expired_handle_stops_being_a_launch_pad`, `test_launch_loses_cleanly_against_a_concurrent_force_cancel` |

## Verification matrix

| ID | Утверждение | Статус | Тест |
|---|---|---|---|
| V1 | Повтор launch не создаёт второго ребёнка | passed | `test_repeat_with_the_same_correlation_id_returns_the_same_child` |
| V2 | Параллельные launch с одним correlationId → ровно одна Task | passed | `test_parallel_launches_with_one_correlation_id_create_one_child` |
| V3 | Launch атомарен: Task + relation + handle | passed | `test_launch_creates_task_relation_and_handle`; сбой любой части откатывает транзакцию целиком (одна транзакция команды) |
| V4 | grant ⊄ effective(parent) → 422 со списком лишних | passed | `test_grant_beyond_the_parent_ceiling_is_rejected`, `test_excess_is_rejected_rather_than_trimmed` |
| V5 | Authoritative write вне потолка → 403 | passed | `test_child_cannot_write_beyond_its_ceiling` |
| V6 | Транзитивность потолка на глубине 3 | passed | `test_ceiling_is_monotone_across_three_levels` (unit), `test_grandchild_cannot_recover_what_its_parent_gave_up` (HTTP) |
| V7 | Token без ключа и с испорченным секретом не разрешается | passed | `test_token_alone_grants_nothing_without_a_key`, `test_resolve_by_id_and_by_token` (подмена секрета → 404) |
| V8 | Чужой tenant не разрешает handle ни по id, ни по token | passed | `test_foreign_tenant_cannot_resolve_a_handle_by_token`, `test_child_handles_are_scoped_to_their_tenant` |
| V9 | Строка результата immutable | passed | `test_a_recorded_result_cannot_be_rewritten` (прямой UPDATE → ошибка триггера) |
| V10 | Повторный terminal transition не перезаписывает результат | passed | `test_a_recorded_result_cannot_be_rewritten` (вторая попытка ребёнка) |
| V11 | Transcript/secrets/абсолютные пути в результате → 422 | passed | `test_result_refuses_transcripts_secrets_and_local_paths` |
| V12 | Превышение границ → 422, Run остаётся running | passed | `test_oversized_child_output_is_rejected_and_the_run_keeps_running` |
| V13 | Zombie parent не запускает ребёнка | passed | `test_launch_requires_the_parent_run_holder_and_a_live_claim` |
| V14 | Событие не содержит summary/data/текста задачи | passed | `test_launch_event_carries_references_only`, `test_child_success_records_a_hashed_bounded_result` (в `run.child.resolved` только outcome, hash, refs) |
| V15 | request_cancel каскадирует по policy; force_cancel — всегда | passed | `test_cooperative_cancel_follows_policy`, `test_force_cancel_cascades_regardless_of_policy` |
| V16 | Revoked/expired handle нельзя использовать для launch | passed | `test_revoke_is_idempotent_and_blocks_further_launches`, `test_expired_handle_stops_being_a_launch_pad` |
| V17 | Reconnect после restart без token | passed | `test_run_context_carries_child_handles_for_reconnect` |
| V18 | resultHash воспроизводим после round-trip через JSONB | passed | `test_result_document_survives_a_json_round_trip_byte_for_byte` |
| V19 | Migration roundtrip upgrade → downgrade → upgrade | passed | `test_manifest_migration_roundtrip`, `test_active_turn_control_migration_roundtrip` (обе теперь проверяют и `run_child_*`), `test_head_matches_code` |
| V20 | Run без handle терминализуется как раньше | passed | `test_a_run_without_a_handle_finishes_exactly_as_before` + весь существующий `tests/integration/test_runs.py` |
| V21 | Глубина дерева ограничена | **частично** | покрыт unit-тестом `test_depth_is_capped`; HTTP-сценарий на 8 уровней не строился — см. «Замечания» |

## Что изменилось в коде

| Слой | Изменение |
|---|---|
| domain | новый `child_handle.py`: narrowing, bounded result + hash, token; `harness_manifest._tool_policy` получил третье измерение `allowedByChildGrant` |
| infrastructure | модели `RunChildHandle`, `RunChildResult`; миграция `a7f2c4d19b60` с immutability-триггером |
| application/commands | новый `child_runs.py` (launch, bind, result, cascade, revoke), новый `_child_ceiling.py` (enforcement); точки вызова в `runs`, `execution`, `artifacts`, `manifests`, `run_controls` |
| application/queries | новый `child_runs.py` (derived status, resolve, list); `childHandles` в Run Context |
| api | `api/v1/child_handles.py`, схемы запросов, регистрация в роутере |
| client/mcp | `launch_child_run`/`list_child_handles`/`resolve_child_handle`/`revoke_child_handle`; MCP `cp_launch_child`, `cp_list_child_handles`, `cp_resolve_child`, `cp_revoke_child`; capability `child_run_handle.v1` |
| docs | SPEC/PLAN/threat model, ADR-0046, api/architecture/harness-protocol/migration |

## Замечания и остаточные риски

- **V21 покрыт только на уровне домена.** Ограничение глубины проверено чистым
  тестом `child_depth`; HTTP-сценарий потребовал бы восьми последовательных
  claim/run пар и проверял бы ту же одну ветку. Оставлено сознательно.
- **Порядок деплоя стал значимым.** `start_run` обращается к
  `run_child_handles` для любого Run, поэтому миграция обязана предшествовать
  новому коду. Тест `test_migration_v07` теперь прогоняет roundtrip именно до
  текущего head, а не до замороженной промежуточной ревизии.
- **R1: handle не выбирает исполнителя.** Дочернюю Task может claim'нуть любой
  eligible principal; потолок применяется к Run под handle.
- **R3: прямой claim дочерней Task в обход handle** оставляет исполнение без
  потолка, но такой Run и не получает handle-семантики. Полное закрытие —
  отдельное решение о запрете claim без handle-контекста.
- **R4: внепротокольный канал между двумя harness** остаётся вне наблюдения;
  закрывается только на уровне execution backend (HRS-1) и sandbox (HRS-6).
- Параллельно шли TASK-000005 (HRS-3) и TASK-000008 (Alembic merge). На момент
  отчёта ветка имеет **одну** Alembic head `a7f2c4d19b60`; при слиянии с
  результатом TASK-000008 нужен rebase либо merge revision — multiple heads не
  считаются готовым состоянием.
