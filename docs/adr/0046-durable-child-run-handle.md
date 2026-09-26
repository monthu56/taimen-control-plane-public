# ADR-0046: Durable Child Run Handle

Статус: Принято (v0.7, TASK-000007)

## Контекст

Дочернее исполнение выражалось только парой «Task + relation `spawned_by`».
Этого хватает для графа и для force-cascade (ADR-0044), но не для
orchestration: launch не идемпотентен, ссылка на ребёнка не переживает restart
оркестратора, родитель не может выдать ребёнку *меньше* прав, чем имеет сам, а
`runs.output` — свободный JSONB без границ, хеша и запрета на перезапись.

Process-local subagent registry решает это в памяти процесса и проваливается
при рестарте, провоцируя перенос transcript и hidden reasoning как «памяти о
запуске». Harness — недоверенный распределённый клиент; источником истины о
дочернем исполнении может быть только Control Plane.

Spike (`tests/unit/test_child_handle.py`) подтвердил два несущих утверждения до
любой схемы: алгебра narrowing монотонна на дереве, а bounded результат даёт
воспроизводимый hash в той же канонической форме, что и манифест HRS-2.

## Решение

Ввести `run_child_handles` — durable locator дочернего исполнения — и
append-only `run_child_results` с триггером immutability.

- **Идемпотентность launch** обеспечивает unique `(parent_run_id,
  correlation_id)`, а не HTTP-заголовок: restart меняет request id, но не
  correlation id. Замок родительской Task берётся до replay-lookup, поэтому
  конкурирующие launch сериализуются, а проигравший читает уже
  зафиксированную строку вместо гонки в constraint.
- **Replay проверяется раньше состояния Run**: восстановление после ambiguous
  response не должно превращать успешный launch в ошибку.
- **Handle не хранит execution status.** Он выводится из дочерних Task/Run на
  каждом чтении. Собственные поля handle — только те, у которых нет другого
  носителя: grant, policy, depth, expiry, revocation.
- **Token — versioned opaque locator** `ch1_<id>_<secret>` с хранением только
  digest'а секрета и constant-time сравнением. Он не является credential:
  каждое обращение заново делает lookup, tenant- и permission-check. Секрет
  отдаётся один раз; потеря не блокирует работу, потому что всё доступно по
  `handleId`.
- **Потолок прав** `granted = requested ∩ effective(parent)`, где для
  дочернего Run `effective` — grant его собственного handle, а не permissions
  его API-ключа. Отсюда монотонность вниз по дереву. Превышение отклоняется
  (`422`), а не срезается молча.
- **Ceiling ограничивает ребёнка, а не надзор за ним.** Оператор, действующий
  на дочерний Run извне, ограничен собственными permissions; это не путь
  эскалации, так как ребёнок не может стать другим principal.
- **Результат пишется внутри терминального перехода** дочернего Run, включая
  force-cascade. Терминального Run без результата не существует. Превышение
  границ — `422`, объём обязан уйти в Artifact.
- **Cancellation policy явная**: `cascade_cooperative` передаёт applied
  `request_cancel` родителя детям через контракт HRS-4; `detach` — нет.
  `force_cancel` каскадирует всегда.

## Рассмотренные варианты

1. **Хранить статус ребёнка на handle.** Отклонено: второй источник истины,
   который неизбежно расходится с Task/Run, и расходится именно тот, которому
   поверили.
2. **Сделать token bearer-credential'ом.** Отклонено: это ввело бы второй
   механизм аутентификации рядом с API-ключами и позволило бы обойти
   permission-check предъявлением строки.
3. **Полагаться только на HTTP `Idempotency-Key`.** Отклонено: ключ привязан к
   запросу, а не к намерению; после restart оркестратор генерирует новый ключ и
   породил бы второго ребёнка.
4. **Молча пересекать запрошенный grant с потолком.** Отклонено: скрывает
   ошибку оркестратора, которая позже проявится как необъяснимо бессильный
   ребёнок.
5. **Хранить результат колонкой на handle.** Отклонено: immutability тогда
   становится дисциплиной кода, а не структурой; отдельная append-only таблица
   с триггером делает перезапись невозможной и позволяет отдельный retention.
6. **Обрезать слишком большой результат.** Отклонено: обрезанный результат
   тоже хешируется, и родитель не отличил бы фрагмент от целого.
7. **Дать `detach` иммунитет от `force_cancel`.** Отклонено: force cancel —
   governance-остановка, а не кооперативный сигнал; иммунитет превратил бы
   policy в способ увести исполнение из-под надзора.

## Последствия

- Две новые таблицы, новый additive HTTP/SDK/MCP surface и capability
  `child_run_handle.v1`.
- **Порядок деплоя стал значимым**: сервер обращается к `run_child_handles` при
  старте любого Run, поэтому миграция накатывается строго до нового кода.
- Дочернюю Task может claim'нуть любой eligible principal; handle ограничивает
  Run под ним, но не выбирает исполнителя (остаточный риск R1 threat model).
- Прямой claim дочерней Task в обход handle оставляет исполнение без потолка
  (R3): закрывается только отдельным решением о запрете такого claim.
- Downgrade уничтожает потолки и результаты, поэтому требует предварительной
  терминализации или отзыва живых handle.

## Проверка решения

- unit: канонизация и hash результата, алгебра narrowing на трёх уровнях,
  token round-trip, границы и guard'ы;
- integration: idempotent launch, атомарность Task+relation+handle, narrowing
  и его enforcement, resolution по id/token, reconnect из Run Context,
  revocation, expiry, bounded immutable result, cancellation policy;
- concurrency: параллельные launch с одним correlation id, launch против
  force_cancel;
- tenant isolation по id и по token;
- migration upgrade → downgrade → upgrade на текущем head;
- regression: существующие Run, cancellation и manifest suites.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"parent_run_id", "correlation_id", name="uq_run_child_handles_parent_correlation"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "run_child_results"'}
  repo: control-plane
- grep: {path: "migrations/versions/*.py", pattern: 'CREATE TRIGGER trg_run_child_results_immutable'}
  repo: control-plane
- grep: {path: src/control_plane/domain/child_handle.py, pattern: 'hmac\.compare_digest\(hash_token_secret'}
  repo: control-plane
- route: "POST /runs/{run_id}/child-handles"
  repo: control-plane
- grep: {path: src/control_plane/domain/enums.py, pattern: '"child_run_handle\.v1"'}
  repo: control-plane
```
