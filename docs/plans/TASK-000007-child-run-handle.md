# TASK-000007: Durable Child Run Handle — PLAN

Статус: выполнено — S0…S6 реализованы, см. [verification report](../verification/TASK-000007-verification.md)

Ветка: `claude/task-000007-child-run-handle`

База: `main` на момент старта; SPEC — `docs/specs/TASK-000007-child-run-handle.md`

## Ограничение параллельной работы

На момент планирования параллельно идут TASK-000005 (HRS-3 Scoped Tool
Discovery View) и TASK-000008 (Alembic merge revision для TASK-000003 /
TASK-000004). Текущая v0.7 head — `d4e6f8a1b2c3`. Эта ветка обязана:

1. брать revision от той head, которая будет актуальной на момент создания
   миграции, а не от зафиксированной здесь;
2. не считать результат готовым при multiple heads — при расхождении делается
   rebase на итог TASK-000008 либо отдельная merge revision;
3. не трогать таблицы и endpoint'ы HRS-3.

## Шаг 0. Spike (без БД и HTTP)

Цель — снять две неопределённости до миграции, как того требуют критерии
перехода от референса к ADR:

- **S0.1 канонизация и hash результата.** Переиспользовать канонический
  сериализатор HRS-2; проверить, что документ результата (`outcome`, `summary`,
  `data`, `artifactRefs`) даёт байт-идентичное представление и стабильный
  `result_hash` на двух прогонах и после round-trip через JSONB.
- **S0.2 алгебра narrowing.** Чистая функция
  `narrow(parent_ceiling, requested) -> granted | error` с проверкой
  транзитивности на дереве глубины 3 и на пустых/пересекающихся множествах.

Выход spike: два модуля + unit-тесты, никаких схем и роутов. Без успешного
spike ADR не пишется.

## Вертикальные TDD slices

### S1. Идемпотентный launch и дочерняя Task

RED:

- integration test: launch создаёт Task, relation `spawned_by` и handle одной
  транзакцией; повтор с тем же `correlationId` возвращает `200` и тот же
  `childTaskId`;
- параллельные launch с одним `correlationId` дают ровно одну дочернюю Task;
- launch без живого Claim родителя → `409 stale_claim`;
- launch на не-running родительском Run → `409 run_not_active`.

GREEN: модель и миграция `run_child_handles`, команда launch, событие
`run.child.launched`, схемы и POST route, генерация/хеширование token.

REFACTOR: вынести lock/idempotency helper, не меняя публичный контракт.

### S2. Narrowing и его enforcement

RED:

- `grant ⊄ effective(parent)` → `422 child_grant_exceeds_parent` со списком
  лишних;
- внук не может расширить потолок родителя (транзитивность);
- authoritative write под дочерним Run вне потолка → `403 child_grant_exceeded`;
- skill, невидимый родителю, невидим ребёнку в манифесте HRS-2.

GREEN: сохранение granted-множеств, вычисление `effective(parent)`, проверка в
authorization-слое дочернего Run, интеграция с `toolPolicy` манифеста.

### S3. Resolution, reconnect и revocation

RED:

- resolution по id и по token; чужой tenant → `404`;
- статус выводится из Task/Run и меняется вслед за ними без записи в handle;
- Run Context отдаёт bounded список активных handle после restart;
- revoke идемпотентен; revoked/expired → `409`, и по нему нельзя launch'ить
  следующий уровень.

GREEN: GET list/resolve, `:revoke`, проекция в Run Context, expiry в чтении.

### S4. Bounded immutable result

RED:

- terminal transition дочернего Run пишет строку результата и `resultHash` в той
  же транзакции;
- повторный/конкурентный terminal transition не перезаписывает результат;
- превышение границ → `422 child_result_too_large`, Run остаётся running;
- UPDATE/DELETE строки результата отклоняются триггером;
- Run без handle терминализуется как раньше.

GREEN: миграция `run_child_results` с immutability-триггером, вычисление
результата в командах `:succeed | :fail | :cancel`, событие
`run.child.resolved`.

### S5. Cancellation policy

RED:

- `request_cancel` родителя при `cascade_cooperative` создаёт control message
  активным детям; при `detach` — нет;
- `force_cancel` каскадирует независимо от policy;
- cascade не трогает уже терминальных детей и идемпотентен при повторе.

GREEN: расширение обработчика control message ссылками на handle policy
поверх существующего `spawned_by` cascade ADR-0044.

### S6. SDK, MCP и документация

- SDK-методы launch/list/resolve/revoke;
- MCP tools `cp_launch_child`, `cp_list_child_handles`, `cp_resolve_child`,
  `cp_revoke_child` — все mutating помечены как требующие решения человека;
- capability `child_run_handle.v1`;
- обновление `docs/api.md`, `docs/architecture.md`, `docs/harness-protocol.md`,
  `docs/migration-v0.7.md`;
- ADR-0046 — **только** после S0 и подтверждения trade-off token vs id.

## Миграции

Две таблицы в одной revision: `run_child_handles`, `run_child_results` +
триггер immutability на второй. Downgrade удаляет обе таблицы и триггер;
`spawned_by` relations и дочерние Task при этом сохраняются — деградация до
поведения v0.7 без handle-семантики.

## Тесты

- unit: канонизация/hash, narrowing, парсинг token, expiry;
- contract: HTTP-коды, error envelope, пагинация, Idempotency-Key;
- concurrency: параллельный launch с одним correlationId, launch против
  force_cancel, конкурентный terminal transition;
- integration: полный цикл launch → claim → run → result → resolution;
- restart: resolution из Run Context без token;
- tenant isolation: чужой handle/token → 404 во всех операциях;
- migration: upgrade → downgrade → upgrade;
- regression: существующие Run/cancellation/manifest suites.

## Compatibility

Additive. Старые harness'ы не объявляют `child_run_handle.v1` и не видят новых
полей. Существующие `spawned_by` Task без handle продолжают работать, включая
force-cascade ADR-0044.

## Rollout

1. Миграция.
2. Сервер с новыми endpoint'ами (без клиентов трафика нет).
3. SDK/MCP и включение capability у harness.
4. Verification report + регистрация артефактов в Control Plane.

## Rollback

Downgrade revision. Перед откатом оператор обязан убедиться, что нет активных
handle с незавершёнными детьми: дочерние Task/Run переживут откат, но потеряют
потолок прав и bounded result. Поэтому откат допустим только после
терминализации или явного revoke активных handle.
