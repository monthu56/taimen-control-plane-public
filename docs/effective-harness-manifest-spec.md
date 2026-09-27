# SPEC: Effective Harness Manifest for Run (HRS-2)

> Исторический документ: ADR-0043 заменён [ADR-0073](adr/0073-agent-registry.md),
> поверхность манифестов удалена (declarative-agents D007, TASK-000592).

Статус: draft (до spike и ADR)
Источник контекста: `docs/reference/hermes-agent.md` верхнеуровневого репозитория,
разделы «Детерминированная сборка prompt/context» и «HRS-2. Effective Harness
Manifest».

## 1. Проблема

Run — единственная точка, где сходятся identity, policy, tool visibility,
budgets и контекст. Сегодня эти входы разбросаны: часть в `sessions`, часть в
effective config проекта, часть в `runs` (budgets), часть вычисляется на лету
(`resolve_executable_skills`, `eventCursor`). Ни один из них не зафиксирован в
момент исполнения.

Следствия:

- нельзя воспроизвести исполнение: конфигурация проекта могла смениться после
  Run, и восстановить «что действовало тогда» невозможно;
- нельзя объяснить, почему конкретный tool был виден Run'у;
- нельзя доказать аудитору, какая governance/budget/model конфигурация
  действовала;
- операционный курсор и Memory Context Pack не разделены на уровне evidence,
  хотя разделены на уровне API (ADR-0028).

## 2. Use cases

| # | Кто | Что делает |
|---|---|---|
| U1 | Аудитор | Открывает завершённый Run и видит immutable снимок effective configuration с provenance каждого элемента |
| U2 | Инженер | Сравнивает два Run одной Task по `baseHash` и мгновенно видит, менялась ли конфигурация |
| U3 | Harness | При старте получает манифест и знает, какие skills видимы и почему |
| U4 | Security review | Доказывает, что в evidence нет secrets, prompt text и transcript |
| U5 | Runtime | При provider fallback фиксирует новую recorded attempt, не переписывая предыдущую |
| U6 | Operator | Видит ephemeral steering/warnings отдельно от frozen base и не путает их с durable intent |

## 3. Модель

### 3.1 Frozen base и captured state

Манифест версии `N` состоит из трёх структурно разделённых частей:

```
base        — frozen: identity, policy revisions, tool policy, budgets, model,
              redaction. Хешируется. Меняется только новой версией.
provenance  — origin каждого ключа base + объяснение видимости каждого tool.
              Детерминированно выводится из тех же входов, хешируется вместе с base.
captured    — operational cursor/версии и ссылка на Memory Context Pack.
              НЕ хешируется: это снимок движущегося состояния, а не revision.
```

`baseHash = "sha256:" + sha256(canonical({schemaVersion, base, provenance}))`.

Разделение — прямое следствие требования 1 acceptance criteria: `eventCursor`
монотонно растёт, и если бы он входил в hash, «те же входные revisions» никогда
не давали бы тот же hash. Курсор фиксируется (он часть evidence), но
воспроизводимость якорится на `baseHash`.

`snapshotHash = "sha256:" + sha256(canonical({schemaVersion, base, provenance, captured}))`
— дополнительный якорь целостности всей строки; для сравнения конфигураций
используется только `baseHash`.

### 3.2 Секции base

| Секция | Содержимое | Источник |
|---|---|---|
| `identity` | tenantId, principalId, principalKind, sessionId, harnessType/Version, protocolVersion, controlLevel, harnessCapabilities | server |
| `run` | runId, taskId, claimId, attempt, fencingToken | server |
| `workerProfile` | `{ref: {id, revision, hash}} \| {status: "unavailable"}` | harness-declared |
| `projectPolicy` | projectId, templateKey, templateVersion, activeRevision, governance, configHash, layers | server |
| `toolPolicy` | список skills с `executableByHarness`, `allowedByGovernance`, `visible`, `reason`; `policyHash` | server |
| `executionBackend` | `{ref: {id, capabilityRevision, hash}} \| {status: "unavailable"}` | harness-declared |
| `model` | requested/effective provider+model, fallbackPolicy, attempt | harness-declared |
| `budgets` | maxDurationSeconds, maxActions, governanceCeiling | server |
| `redaction` | policy, version | server default + harness-declared override |

`workerProfile` и `executionBackend` — forward-compatibility заглушки: HRS-1 и
Worker Profile ещё не реализованы. Они присутствуют в схеме с явным
`status: "unavailable"`, чтобы появление подсистемы не ломало схему манифеста и
не требовало миграции формата.

### 3.3 Секции captured

```json
{
  "operational": {
    "eventCursor": "ec1_…",
    "taskVersion": 7,
    "claimEpoch": 3,
    "runAttempt": 1,
    "capturedAt": "2026-08-12T19:16:04.219744+00:00"
  },
  "memory": null
}
```

`memory` — либо `null`, либо `{packId, provenance, freshness}` **без содержимого
пакета**: только идентификатор, происхождение и метрика свежести. Контент
Context Pack не копируется в манифест никогда (он eventually consistent и
принадлежит Memory Service).

Операционная и memory-части никогда не сливаются в один объект — это то же
структурное разделение, что и в ADR-0028, применённое к evidence.

### 3.4 Ephemeral additions

Steering, budget warnings и прочие временные добавления записываются
**отдельными append-only строками**, ссылающимися на версию манифеста, в силе на
момент записи. Они:

- не входят ни в `base`, ни в `provenance`, ни в один из хешей;
- отдаются API в отдельном поле `ephemeral`, никогда не смешиваясь с `base`;
- имеют собственный `seq`, `kind` и `createdBy`.

Это делает невозможным «тихое» изменение frozen base: любое изменение base
требует новой версии с новым `baseHash`, любая ephemeral запись видна как
ephemeral.

### 3.5 Версионирование и provider fallback

- `version` монотонен в пределах Run, назначается под row lock Run.
- Compile идемпотентен по содержанию: если пересчитанный `baseHash` равен
  `baseHash` активной версии — новая строка не создаётся, возвращается активная
  (HTTP 200). Если отличается — создаётся `version + 1` (HTTP 201).
- `compileReason ∈ {run_started, recompile, provider_fallback}`.
- Provider fallback обязан вызывать compile с `reason=provider_fallback` и
  инкрементом `model.attempt`. Так как `model.attempt` входит в `base`, fallback
  всегда даёт новый `baseHash` и, следовательно, **новую recorded attempt** —
  даже если провайдер-заменитель совпал по имени.
- `supersedesVersion` указывает на версию, поверх которой скомпилирована новая.

## 4. Server-authoritative boundary

| Данные | Кто авторитетен |
|---|---|
| identity, run, projectPolicy, toolPolicy, budgets, operational captured | **сервер**, из БД в той же транзакции |
| workerProfile, executionBackend, model, redaction override | клиент **декларирует**, сервер записывает с `source: "harness_declared"` |
| memory captured | клиент передаёт ссылку, полученную от Memory Service; сервер не проверяет содержимое |

Клиент не может подменить server-authoritative секцию: попытка передать
`identity`, `run`, `projectPolicy`, `toolPolicy` или `budgets` в теле compile
отклоняется `422 server_authoritative_section`.

`harness_declared` — это не «доверенные данные», а **запись заявления** с
provenance. Ни одно authorization-решение не принимается на их основе.

## 5. Инварианты

| ID | Инвариант |
|---|---|
| I1 | Одинаковые входы → байт-идентичное каноническое представление → одинаковый `baseHash` |
| I2 | Новая версия создаётся тогда и только тогда, когда меняется `baseHash` |
| I3 | Строка манифеста immutable: UPDATE и DELETE запрещены триггером БД |
| I4 | `captured.memory` и `captured.operational` структурно разделены; ни то, ни другое не входит в `baseHash` |
| I5 | Ephemeral записи не меняют `base`, `provenance` и хеши |
| I6 | Ни в одной сохранённой секции нет secrets, prompt text, transcript, chain-of-thought и абсолютных локальных путей |
| I7 | Server-authoritative секции не принимаются от клиента |
| I8 | Манифест виден только внутри своего tenant; чужой `runId` → 404 |
| I9 | Манифест принадлежит ровно одному Run; цепочки версий разных Run одной Task независимы |
| I10 | Compile требует живой claim и running Run того же principal (fencing gate, как у checkpoint) |

## 6. Каноническое представление

`canonical_bytes(value)`:

1. JSON, UTF-8, `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`;
2. все строки нормализуются в Unicode NFC;
3. порядок элементов массива значим и сохраняется;
4. `null` сохраняется: явное отсутствие — это содержание;
5. **float отклоняется** (`ValidationError`). Причина: `0.1` и `1e-1` — одно
   значение с разными представлениями, а repr float не переносим между
   языками. Все числа манифеста — целые (budgets, versions, attempts);
6. отклоняются: не-строковые ключи, NaN/Inf, `bytes`, `set`, `datetime`
   (сериализуется вызывающим в ISO-8601 заранее);
7. применяются существующие guard'ы размера/глубины (`guard_json_document`).

Хеш — `sha256`, префикс алгоритма в значении (`sha256:…`) для будущей смены
алгоритма без миграции формата.

## 7. Data contract

Таблица `run_harness_manifests`:

| Колонка | Тип | Замечание |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK tenants | |
| `run_id` | uuid FK runs | |
| `task_id` | uuid FK tasks | денормализация для запросов по Task |
| `project_id` | uuid NULL | проект на момент компиляции |
| `version` | int ≥ 1 | UNIQUE (run_id, version) |
| `base_hash` | text | CHECK `^sha256:[0-9a-f]{64}$` |
| `snapshot_hash` | text | тот же CHECK |
| `base` | jsonb | frozen |
| `provenance` | jsonb | origin map |
| `captured` | jsonb | operational + memory |
| `compile_reason` | text | CHECK IN (run_started, recompile, provider_fallback) |
| `model_attempt` | int ≥ 1 | |
| `supersedes_version` | int NULL | |
| `created_by` | uuid FK principals | |
| `created_at` | timestamptz | |

Таблица `run_manifest_ephemerals`:

| Колонка | Тип | Замечание |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK tenants | |
| `run_id` | uuid FK runs | |
| `manifest_id` | uuid FK run_harness_manifests | версия, действовавшая на момент записи |
| `seq` | int ≥ 1 | UNIQUE (manifest_id, seq) |
| `kind` | text | CHECK IN (steering, warning, budget_warning, note) |
| `summary` | text | ≤ 500 символов |
| `data` | jsonb | bounded, secret-guarded |
| `created_by` | uuid FK principals | |
| `created_at` | timestamptz | |

Обе таблицы append-only: триггер отклоняет UPDATE и DELETE (тот же приём, что у
`project_config_revisions`, ADR-0032).

## 8. API contract

| Метод | Путь | Семантика |
|---|---|---|
| `GET` | `/api/v1/runs/{run_id}/harness-manifest` | активная версия; `?version=N` — конкретная |
| `GET` | `/api/v1/runs/{run_id}/harness-manifests` | список версий (summary) |
| `POST` | `/api/v1/runs/{run_id}/harness-manifest:compile` | 200 — без изменений, 201 — новая версия |
| `POST` | `/api/v1/runs/{run_id}/harness-manifest/ephemeral` | 201 — запись ephemeral marker |

Compile тело:

```json
{
  "reason": "recompile",
  "workerProfile": {"id": "...", "revision": 4, "hash": "sha256:..."},
  "executionBackend": {"id": "local-process", "capabilityRevision": 2, "hash": "sha256:..."},
  "model": {
    "provider": "anthropic", "model": "claude-opus-5",
    "fallbackPolicy": {"order": ["primary", "secondary"], "maxAttempts": 2}
  },
  "redaction": {"policy": "default", "version": 1},
  "memory": {"packId": "ctx-8c22c173", "provenance": "memory-service", "lagEvents": 0}
}
```

`memory` — единственное поле тела, попадающее не в `base`, а в `captured`:
это ссылка на Context Pack, полученная клиентом от Memory Service. Она
проходит те же guard'ы (размер, secrets, пути), но не влияет на `baseHash`.

Ответ GET:

```json
{
  "manifest": {
    "id": "...", "runId": "...", "version": 2,
    "baseHash": "sha256:...", "snapshotHash": "sha256:...",
    "compileReason": "provider_fallback", "modelAttempt": 2,
    "supersedesVersion": 1, "createdAt": "...",
    "base": {...}, "provenance": {...}, "captured": {...}
  },
  "ephemeral": [{"seq": 1, "kind": "warning", "summary": "...", "data": {...}, "createdAt": "..."}]
}
```

Compile — write через существующий idempotency write flow (`Idempotency-Key`),
как checkpoint и run action.

Авторизация:

- чтение — `tasks.read` (как run context);
- compile и ephemeral — `tasks.claim` + Run в статусе `running` + `principal_id`
  Run равен вызывающему + живой claim с совпадающим fencing token.

Манифест v1 компилируется **автоматически** внутри транзакции `start_run` с
`reason=run_started`. Run без манифеста не существует.

## 9. Event contract

| Событие | entity | payload |
|---|---|---|
| `run.manifest_compiled` | `run` | `{taskId, manifestId, version, baseHash, reason, modelAttempt, supersedesVersion}` |
| `run.manifest_ephemeral_recorded` | `run` | `{taskId, manifestId, version, seq, kind}` |

Payload событий не содержит ни `base`, ни `data` ephemeral записи — только
ссылки и хеши (ADR-0019: аудит ссылается, а не копирует).

## 10. MCP projection

Read-only tool `cp_harness_manifest(run_id?)` — проекция активного манифеста
для harness: `identity`, `toolPolicy` с объяснением видимости, `budgets`,
`baseHash`, `captured.operational`, список ephemeral. Mutating-проекции в v1 нет:
compile выполняет сам harness через HTTP при старте run и при fallback.

## 11. Out of scope

- Prompt text, transcript, hidden reasoning, raw prompts — не хранятся нигде.
- Credentials — только `secretRef`, и то не в манифесте.
- Реализация model provider и его fallback-механики (манифест только фиксирует
  заявленную политику и attempt).
- Context Compiler и содержимое Context Pack.
- Реализация Worker Profile (HRS) и Execution Backend (HRS-1) — только ссылки.
- Унификация tool discovery (HRS-3): манифест объясняет видимость, но не меняет
  правила её вычисления.

## 12. Совместимость

- Существующие Run без манифеста остаются валидными: чтение возвращает `404
  manifest_not_found`, а не 500. Манифест обязателен только для Run, стартовавших
  после миграции.
- `schemaVersion` внутри документа отделён от версии манифеста Run: смена схемы
  не переписывает историю, старые версии читаются по их собственному
  `schemaVersion`.
- Downgrade миграции удаляет обе таблицы; это удаление evidence, поэтому в
  production-runbook downgrade сопровождается выгрузкой (см. PLAN, rollback).
