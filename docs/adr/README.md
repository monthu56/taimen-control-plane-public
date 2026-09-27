# Реестр архитектурных решений

Формат: `NNNN-kebab-case-slug.md`, статус и последствия внутри каждого файла.
Историю не переписываем — устаревшее решение заменяется НОВЫМ ADR.

## v0.1 — основание

| # | Решение |
|---|---|
| [0001](0001-postgresql-source-of-truth.md) | PostgreSQL — единственный источник истины |
| [0002](0002-modular-monolith.md) | Модульный монолит вместо микросервисов |
| [0003](0003-state-plus-events.md) | Состояние + неизменяемые события, не event sourcing |
| [0004](0004-lease-fencing.md) | Lease + fencing token для конкурентной работы |
| [0005](0005-transactional-outbox.md) | Transactional outbox для доставки событий |
| [0006](0006-api-key-auth.md) | API-key аутентификация в MVP |
| [0007](0007-no-redis-celery.md) | Без Redis/Celery в первой версии |

## v0.2 — организационная модель

| # | Решение |
|---|---|
| [0008](0008-workspace-hierarchy.md) | Иерархия workspaces: adjacency list + advisory lock |
| [0009](0009-role-capability-skill.md) | Семантика Role vs Capability vs Skill |
| [0010](0010-task-requirements-eligibility.md) | Task requirements и eligibility при claim |
| [0011](0011-task-dependency-model.md) | Модель зависимостей задач и readiness |
| [0012](0012-run-ownership-fencing.md) | Run: ownership и fencing-семантика |
| [0013](0013-artifact-model.md) | Artifact: reference-модель хранения; амендмент 2026-09-26 (ADR-0072): содержимое в хранилище ядра |
| [0014](0014-approval-model.md) | Approval: минимальный примитив губернанса |

## v0.3 — harness protocol и execution runtime

| # | Решение |
|---|---|
| [0015](0015-harness-as-session-metadata.md) | Harness — метаданные Session, не отдельный aggregate |
| [0016](0016-claude-code-mcp-integration.md) | Harness Protocol поверх REST + событий; Claude Code через MCP |
| [0017](0017-local-harness-authentication.md) | Аутентификация локального harness |
| [0018](0018-approval-gate-run-suspension.md) | Approval gate + suspension |
| [0019](0019-execution-audit-run-actions.md) | Execution audit (RunAction) отдельно от журнала |
| [0020](0020-artifact-revisions.md) | Ревизии артефактов: supersedes-цепочка |
| [0021](0021-skill-version-resolution.md) | Резолюция версий skills |

## v0.4 — надёжный курсор, память, white-label

| # | Решение |
|---|---|
| [0022](0022-white-label-naming.md) | White-label нейтрализация имён |
| [0023](0023-reliable-event-cursor.md) | Надёжный replay-курсор — порядок `(tx_id, sequence)` |
| [0024](0024-opaque-event-cursor.md) | Непрозрачный публичный EventCursor |
| [0025](0025-external-context-memory-boundary.md) | Внешний Context Memory Engine за HTTP-границей |
| [0026](0026-at-least-once-observation-delivery.md) | At-least-once доставка Observations |
| [0027](0027-explicit-replayable-observations.md) | Явные replayable-наблюдения («remember») |
| [0028](0028-operational-vs-durable-context.md) | Operational context vs durable memory |

## v0.5 — Project Model и reliability backlog

| # | Решение |
|---|---|
| [0029](0029-workspace-types.md) | Workspace Types как типизированное ограничение дерева |
| [0030](0030-immutable-project-templates.md) | Неизменяемые версионированные Project Templates |
| [0031](0031-project-profile-and-lifecycle.md) | Project Profile поверх Workspace и настраиваемый lifecycle |
| [0032](0032-versioned-project-configuration.md) | Версионированная конфигурация и effective config |
| [0033](0033-governance-lattice.md) | Типизированная решётка governance и запрет ослабления |
| [0034](0034-external-references.md) | Generic external references как mapping |
| [0035](0035-project-scope-resolution.md) | Принадлежность задачи проекту выводится из дерева |
| [0036](0036-per-tenant-delivery-isolation.md) | Per-tenant изоляция доставки Context Adapter |
| [0037](0037-operator-redrive.md) | Операторский redrive без возможности пропуска |
| [0038](0038-event-journal-retention.md) | Retention, архив и rebuild журнала событий |
| [0039](0039-trace-run-id.md) | Сквозной trace-идентификатор `X-Run-Id` |
| [0040](0040-end-of-compatibility-window.md) | Закрытие compatibility window кодового имени |
| [0041](0041-opencode-harness-adapter.md) | OpenCode как harness-адаптер |

## v0.6 — Human Operator Harness Pilot

| # | Решение |
|---|---|
| [0042](0042-human-operator-harness-handoff.md) | Product-neutral operator harness и атомарный handoff |

## v0.7 — Agent Runtime Contracts

| # | Решение |
|---|---|
| [0043](0043-effective-harness-manifest.md) | Effective Harness Manifest как immutable evidence Run (→ Superseded, [ADR-0073](0073-agent-registry.md)) |
| [0044](0044-durable-active-turn-control.md) | Durable Active Turn Control как Run subresource |
| [0045](0045-scoped-tool-discovery.md) | Scoped Tool Discovery View и re-authorization при исполнении |
| [0046](0046-durable-child-run-handle.md) | Durable Child Run Handle: locator, потолок прав и bounded result |

## v0.8 — Модель work item

| # | Решение |
|---|---|
| [0047](0047-generic-external-references.md) | Реестр entity bindings для external references |
| [0048](0048-work-item-type-and-lifecycle.md) | Реестр task_types, пара «ключ + категория» и настраиваемый lifecycle |
| [0049](0049-work-item-fields-dates-filters.md) | Custom fields, плановые даты и расширенная выборка work item |
| [0050](0050-work-item-comments.md) | Комментарии к work item и append-only история правок |

## v0.9 — Наблюдаемость исполнения

| # | Решение |
|---|---|
| [0051](0051-execution-trace-transcript-artifact.md) | Execution trace: транскрипт как bounded артефакт и run action на вызов инструмента |
| [0052](0052-auto-review-by-runner-daemon.md) | Автоматическое ревью кода: задачу ревьюеру заводит демон runner'а, вердикт из summary — в поля задачи |

## v0.10 — Identity

| # | Решение |
|---|---|
| [0053](0053-iam-identity-source-and-binding-api.md) | IAM — источник identity, API-ключ — только bootstrap и аварийный путь; bindings управляются API (ADR-0006 → Superseded) |

## v0.11 — Память и авторизация

| # | Решение |
|---|---|
| [0054](0054-governed-graph-memory.md) | Структурированная графовая память через Control Plane: assertions и anchors в `cp_remember` / `cp_get_context` |
| [0055](0055-policy-authorize-and-shadow-mode.md) | `authorize()` и внешний PDP policy-service: режимы local / shadow / policy, IAM principal как субъект, `iam_actor_id` в журнале |
| [0056](0056-skill-contract-and-invocation.md) | Skill contract v1 (иммутабельная версия, side effects, retry, idempotency) и вызов Skill ядром через долговечный `skill_invocation` и исполнителя Skills |
| [0057](0057-external-observation-intake.md) | Приём внешних наблюдений: `source`, `dedupKey` (дедуп 201 → 200 по tenant+source+dedupKey), `observedAt`, `supersedes`, `externalRef` |
| [0058](0058-reject-unknown-query-parameters.md) | Неизвестный query-параметр — `400 invalid_request` на всём `/api/v1`, а не тихо проигнорированный фильтр |
| [0059](0059-task-context-pack-reaches-the-agent.md) | Пакет контекста доходит до агента: общий рендер «Контекст задачи» с бюджетом для всех адаптеров, описание задачи в retrieval, namespace корневого workspace и `asOf` в запросе к Memory |
| [0060](0060-knowledge-snapshots-through-control-plane.md) | Снимки знаний и доменные пакеты через ядро: `POST /knowledge/snapshots`, `/knowledge/packs`, `PUT /workspaces/{id}/knowledge-packs`; namespace корня дерева и scope workspace выводит ядро |
| [0061](0061-approval-outcomes-declared-by-task-type.md) | Исходы approval объявляет тип задачи (`approval_schema`): закрытый словарь действий `ensureWork` / `completeTask` / `comment` / `transition` / `invokeSkill`, исполнитель в worker'е с полномочиями решившего, идемпотентность по (approval, индекс), replay; `code-review` v3; `invokeSkill` через `skill_invocation`, задача закрывается по итогу вызова (`onSuccess`/`onFailure`); предусловия `approve` по наблюдениям (`409 approval_precondition_failed`) |

## M1 — Work graph

| # | Решение |
|---|---|
| [0062](0062-work-graph-goal-origin-acceptance.md) | Work graph (TAI-ADR-0035): Goal как нейтральная сущность ядра, `goalId` / неизменяемый `origin` (`human`/`harness`/`rule`+`ruleId`+evidence/`parent`/`process`/`external`) / `acceptance` / `evidence` (observation, artifact, external) у задачи, права `goals.read`/`goals.write` |
| [0063](0063-work-derivation-rules.md) | Правила вывода работы (TAI-ADR-0036): `work_rules` / `rule_evaluations` как данные тенанта, ограниченный язык JSON-выражений, триггеры observation/event/schedule по своему курсору журнала, интерпретация через `skill_invocation` без блокировки worker'а (результат — артефакт-evidence), `ensure_work` / `update_work` / `cancel_work` / `request_decision` с общим для tenant'а ключом, полномочия включившего, `rule.evaluated` / `work.derived` / `work.reconciled`, права `rules.read` / `rules.write`, вид `WorkRule` для пакетов; амендмент 2026-09-25 (M1.6): `complete_work` через стадию проверки, `acceptance` у `ensure_work`, evidence решения в задаче, отложенное решение под claim с `request_cancel_run` |
| [0064](0064-task-context-profile-and-pack.md) | Профиль контекста типа задачи (`context_schema`: anchors/traverse/asOf/budgetTokens, TAI-ADR-0042 P2): кандидаты якорей по `idPatterns` пакетов, типизированный обход Memory, пакет на claim записывается (`task_context_packs`, без записи в задачу; цитировать его в evidence можно явно), `GET /context-packs/{id}` и `:replay` (`events.read`, редакция якорей недоступных источников); `POST /context/recall` и MCP `cp_recall` |
| [0065](0065-break-glass-key.md) | Аварийный ключ (break-glass): legacy-ключ с префиксом `bg`, выпускается только из shell на хосте (`python -m control_plane.break_glass`), только человеку, `admin` на ≤ 4 ч с причиной в журнале; принимается и при закрытом окне legacy-ключей; `CP_BREAK_GLASS_ENABLED` |
| [0066](0066-task-type-instructions.md) | Инструкции исполнителю у типа задачи (TAI-ADR-0009, частично): поле `instructions` версии типа, слой проекта `settings.agentInstructions`, порядок слоёв платформа → проект → тип → репозиторий, блок `instructions` в контексте run, общий рендер для всех адаптеров, хэш на run |
| [0067](0067-verification-stage.md) | Стадия проверки (M1.6): критерии приёмки исполняются до «выполнено» — стадия, а не статус; попытки `task_verifications`, порядок объявления, провал возвращает задачу исполнителю, третий подряд — `blocked`; `human`/`llm_judge` — решение gate-approval; грамматика `acceptance[].spec` по видам (`422 invalid_acceptance_spec`); `task.verification_started`/`task.verified`/`task.verification_failed`, `GET /tasks/{ref}/verifications`, причина `verification_pending`; нейтральность с пробой; амендмент 2026-09-26 (ADR-0072): критерий `deterministic` с `artifact`, обязательные выходы типа — неявные критерии `output.<key>` перед acceptance |
| [0068](0068-event-filters-catalog-versions.md) | Журнал событий как подписка (фича notifications, N002): `types` (префиксы) и `workspaceId` в `GET /events` и `WS /events/ws`, `events.read` на workspace, `workspaceId` и `schemaVersion` в конверте (колонки журнала), реестр `domain/event_catalog.py` → `docs/events/`, `record_event` отказывает в неизвестном типе, версии только добавляют поля; payload `approval.*` v2; `GET /roles/{id}/principals?workspaceId=`; амендмент: approval задачи живёт в workspace задачи (явный — только предок) |
| [0069](0069-event-consumer-sdk.md) | SDK потребителя событий (фича notifications, N003): `control_plane_client.events.EventConsumer` — событие как единица работы хранилища курсора (отметка `event.id` + курсор вместе), протокол `CursorStore`, `SqlAlchemyCursorStore` (эффекты обработчика в той же транзакции), повтор упавшей страницы с паузой, отказ в подписке — исключение; WebSocket только будит, опрос раз в 30 с; `to_cloudevent` (CloudEvents 1.0); экстры `[ws]`/`[sqlalchemy]`/`[events]` |
| [0070](0070-channel-decision-scope.md) | Решение из канала (фича notifications, N005): scope `control-plane:decide` оставляет только `approvals.decide`, `purpose_ref=approval:<id>` пускает только на `:approve`/`:reject` этого approval (`403 outside_purpose` / `purpose_ref_required`), потолок держится и в режиме `policy`; `acr=channel:<имя>` → `channel` в `approval.approved|rejected`; `Idempotency-Key` обязателен |
| [0071](0071-attention-list.md) | «Важное» человека (фича human-harness, H003): `GET /me/attention?workspaceId=&includeDescendants=` — правила `ruleKey@version` (`approval.review`, `approval.decide`, `task.due_not_started` ≤ 48 ч, `task.blocked`, `task.delegated_failing` ≥ 3 провалов подряд) с `itemKey`/`kind`/`reasonCode`/`score`/`actions`, чужое не видно по построению, правило падает в `degraded[]`, а не весь ответ; `POST /me/attention/{itemKey}:feedback` (`useful`/`not_needed`, таблица `attention_feedback`, событие `attention.feedback_recorded`) не меняет список |
| [0072](0072-artifact-content-types-task-io.md) | Содержимое артефактов, типы артефактов, входы и выходы типа задачи (фича artifact-handoff, A002): поток через API — `PUT /artifact-contents` → `contentRef` (привязан к загрузившему, 24 ч) → `POST /artifacts`; порт `ContentStore` (S3-совместимый, объект по sha256 внутри tenant, дедупликация), выдача `GET /artifacts/{id}/content` с авторизацией на задаче и `?forTask=`, `artifact.content_read`; `:purge-content` администратора; реестр `artifact-types`; `artifactSchema` типа задачи (входы по `depends_on`/`spawned_by`/`parent`, выходы), `inputs` в контексте, `409 input_missing`; `503 content_store_unavailable` |
| [0073](0073-agent-registry.md) | Реестр агентов (фича declarative-agents: контракт — D002, реализация — D005): `POST /agents {key, spec}` — неизменяемая ревизия только при отличии `sha256` канонического JSON `spec` (без `state`/`placement.replicas`), `:validate`, `key@revision`; желаемое состояние `PATCH …/state` отдельно от ревизии; фактическое `PUT …/status` пишет только `agents.status.write` (fleet-controller); права `agents.read/manage/status.write`, права ревизии ⊆ права применяющего (`403 permission_escalation`); личность — `PUT …/identity`, principal и связку по ревизии держит ядро; `runs.agent_revision_id` и `agentRevisionId` в `start-run`; `GET /agents/me`; `:retire`; демон исполнителя из ревизии (D006): режим агента, выход 75 при смене ревизии, `drainSeconds` → отмена `drained`; события `agent.revision_published|state_changed|status_changed|retired`; манифесты харнесса удалены (D007, ADR-0043 → Superseded); `identity.iam {audiences, scopeCeiling}` — данные для выпускающего сервисную учётку, ядро не толкует (D014a) |
