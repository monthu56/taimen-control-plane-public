# ADR-0043: Effective Harness Manifest как immutable evidence Run

Статус: Superseded — [ADR-0073](0073-agent-registry.md) (заменён CP-ADR-0073,
2026-09-27): конфигурация исполнителя — ревизия агента, прогон называет её в
`agentRevisionId`. Поверхность удалена в declarative-agents D007 (TASK-000592):
таблицы `run_harness_manifests` и `run_manifest_ephemerals` сносит миграция
`439255fb8627`, маршруты `/runs/{id}/harness-manifest*`, метод клиента и MCP-tool
`cp_harness_manifest` удалены. Типы событий `run.manifest_compiled` и
`run.manifest_ephemeral_recorded` остаются в каталоге для событий, уже лежащих
в журнале, но больше не пишутся. Принято (v0.7).

Предпосылка: spike HRS-2 выполнен и зелёный (`tests/unit/test_harness_manifest.py`),
trade-offs подтверждены. Контракт — `docs/effective-harness-manifest-spec.md`,
план — `docs/effective-harness-manifest-plan.md`, угрозы и матрица проверки —
`docs/effective-harness-manifest-threat-model.md`.

## Контекст

Run сводит воедино identity, project policy, видимость tools, budgets, модель и
контекст, но нигде не фиксировал их **как снимок**. Конфигурация проекта
версионирована (ADR-0032), skills резолвятся на лету, курсор журнала движется —
и после завершения Run восстановить «что действовало тогда» было невозможно.
Это ломает воспроизводимость, аудит и возможность объяснить, почему конкретный
tool был виден.

## Решение

Каждый Run получает **Effective Harness Manifest** — immutable, версионированный
снимок effective runtime configuration, компилируемый сервером.

### Frozen base против captured state

Документ разделён на три части, и это разделение — суть решения:

- `base` + `provenance` — frozen: identity, revisions/hashes политик, tool policy
  с объяснением видимости, budgets, model, redaction. Хешируется в `baseHash`.
- `captured` — движущееся состояние: operational cursor/версии и **ссылка** на
  Memory Context Pack. В `baseHash` не входит.

Причина: `eventCursor` монотонно растёт. Если бы он входил в hash, требование
«одни и те же revisions дают один и тот же hash» было бы невыполнимо в принципе.
Курсор при этом остаётся частью evidence — он просто не якорь воспроизводимости.
Memory лежит в собственной ветке `captured.memory`: eventually consistent данные
не должны быть неотличимы от авторитетного операционного состояния (ADR-0028),
в том числе внутри evidence.

### Каноническое представление

JSON с сортировкой ключей, без пробелов, NFC, UTF-8; hash — `sha256:<hex>` с
алгоритмом внутри значения. **Float отклоняется**: `0.1` и `1e-1` — одно
значение с двумя представлениями, а repr float не переносим между языками.
Манифесту дробные числа не нужны, поэтому запрет убирает целый класс
рассогласования hash между реализациями вместо того, чтобы его маскировать.

### Версия = изменение base

Compile идемпотентен по содержанию: совпал `baseHash` — возвращается активная
версия (200), не совпал — создаётся следующая (201). Поэтому «новая версия»
означает «конфигурация действительно изменилась», а не «кто-то вызвал compile».

### Provider fallback

`reason=provider_fallback` обязан объявить `model.attempt` больше активного.
`attempt` входит в `base`, поэтому fallback всегда создаёт новую recorded
attempt — даже при том же провайдере. Тихий fallback, замаскированный под
обычный recompile, отклоняется (`422 invalid_fallback_attempt`).

### Ephemeral отдельно

Steering и warnings — append-only строки отдельной таблицы, ссылающиеся на
версию манифеста. Они не входят ни в `base`, ни в хеши и отдаются отдельным
полем ответа. Frozen base не может измениться незаметно.

### Server-authoritative boundary

`identity`, `run`, `projectPolicy`, `toolPolicy`, `budgets` считает сервер;
попытка передать их в теле — `422 server_authoritative_section`.
`workerProfile`, `executionBackend`, `model`, `redaction` harness **декларирует**
— сервер записывает их с provenance `harness_declared`. Это запись заявления, а
не источник полномочий: ни одно authorization-решение на них не опирается.

### Tool visibility

Манифест сообщает два независимых измерения — `executableByHarness` (протокол
поддержан harness'ом) и `allowedByGovernance` (протокол разрешён governance), —
и `visible` как их конъюнкцию, с причиной для каждого tool. Манифест не может
противоречить run context: он добавляет измерение, а не переопределяет резолвер.

## Альтернативы

- **Хешировать весь документ вместе с курсором.** Отвергнуто: делает
  воспроизводимость недостижимой по построению.
- **Мутировать активный манифест при изменении конфигурации.** Отвергнуто:
  evidence, которое можно править на месте, ничего не доказывает.
- **Ephemeral внутри манифеста с флагом.** Отвергнуто: один забытый фильтр в
  читателе — и временная коррекция выглядит как durable intent.
- **Компилировать манифест лениво, по запросу.** Отвергнуто: Run без evidence
  создаёт молчаливую дыру в аудите, а поздняя компиляция зафиксирует уже другую
  конфигурацию.

## Последствия

- Каждый Run стоит одной дополнительной строки и нескольких чтений в транзакции
  старта. Отказ компиляции = отказ старта Run: это осознанный обмен доступности
  на полноту аудита.
- `alembic downgrade` удаляет обе таблицы, то есть **evidence**. Runbook требует
  выгрузки перед downgrade; технически иначе не сделать без внешнего хранилища.
- `harness_declared` секции недоказуемы: сервер фиксирует заявление, но не может
  подтвердить, что harness действительно использовал заявленную модель. Полное
  решение требует attestation от execution backend (HRS-1). Provenance честно
  называет источник, чтобы ограничение было видно аудитору, а не подразумевалось.
- Схема документа версионирована `schemaVersion` отдельно от версии манифеста:
  смена формата не переписывает историю.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»). После замены
ADR-0073 они проверяют, что поверхность удалена; канонический JSON остаётся
(его используют discovery и реестр агентов):

```conformance
- absent: {path: "src/control_plane/**/*.py", pattern: '__tablename__ = "run_harness_manifests"'}
  repo: control-plane
- absent: {path: "src/control_plane/**/*.py", pattern: 'harness-manifest'}
  repo: control-plane
- grep: {path: src/control_plane/domain/canonical.py, pattern: 'Floating point numbers are not allowed'}
  repo: control-plane
```
