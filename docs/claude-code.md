# Claude Code + Control Plane: human operator harness

Claude Code — один из взаимозаменяемых human operator harness Control Plane.
Codex использует тот же adapter и описан отдельно в [codex.md](codex.md).
Интеграция — через
MCP (официальный механизм расширения Claude Code, ADR-0016): stdio MCP-сервер
`control-plane-mcp` поверх официального SDK `control_plane_client` поверх
REST API.

```
Claude Code ──MCP/stdio──▶ control-plane-mcp ──control_plane_client──▶ Control Plane HTTP API
```

MCP-сервер — stateless-адаптер: бизнес-инварианты и авторизация остаются на
Control Plane; official-клиентство не даёт Claude Code никакого специального
доверия.

## Установка

1. Установить пакет (в virtualenv проекта control-plane или `uv tool install`):

   ```bash
   uv sync   # в репозитории: даёт control-plane и control-plane-mcp
   ```

2. Привязать репозиторий к серверу (несекретные метаданные, можно коммитить):

   ```bash
   control-plane init --server https://cp.example.com --workspace engineering \
       --repository git@github.com:acme/x.git
   ```

3. Авторизоваться (ключ попадает в macOS Keychain или
   `~/.config/control-plane/credentials.json` c правами 0600 — никогда в repo):

   ```bash
   control-plane login --server https://cp.example.com
   ```

4. Добавить project-scoped `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "control_plane": {
         "type": "stdio",
         "command": "control-plane-mcp",
         "args": [],
         "env": {
           "CONTROL_PLANE_HARNESS_TYPE": "claude-code",
           "CONTROL_PLANE_HARNESS_VERSION": "project-v0.6",
           "CONTROL_PLANE_HARNESS_CLIENT_NAME": "claude-code-operator"
         }
       }
     }
   }
   ```

   Секретов в конфиге MCP нет: сервер сам резолвит ключ из credential store,
   а адрес — из `.control-plane/config.json` (или `CONTROL_PLANE_SERVER`).

5. В `.gitignore` проекта (если используете файловые секреты рядом):

   ```
   .control-plane/*.local.json
   ```

## MCP tools

| Tool | Что делает |
|---|---|
| `cp_whoami` | tenant, principal, permissions |
| `cp_context` | полный bootstrap-контекст + локальное состояние процесса |
| `cp_get_context` | рабочий контекст: authoritative state + durable memory |
| `cp_recall` | граф знаний workspace: от идентификатора (`anchor`) или из текста (`query`) по связям (`relations`, `direction`, `depth`) на момент (`as_of`); тот же рендер, что раздел «Контекст задачи» (ADR-0064) |
| `cp_remember` | сохранить явный finding/decision в долговременную память |
| `cp_list_work` | доступные мне задачи (advisory) |
| `cp_create_task` / `cp_list_tasks` / `cp_update_task` | operator backlog и optimistic update; `custom_fields` по схеме типа, плановые даты, фильтры по owner и датам, `sort=dueDate` |
| `cp_add_task_relation` / `cp_remove_task_relation` | структура parent/dependency с server cycle checks |
| `cp_get_task` | задача + диагностика claimability + допустимые цели перехода |
| `cp_list_task_types` / `cp_get_task_type` | реестр типов work item, read-only: словарь статусов и граф переходов тенанта |
| `cp_create_goal` / `cp_update_goal` / `cp_list_goals` / `cp_get_goal` | цели (ADR-0062): желаемое состояние, критерии, работа цели, закрытие цели (`status` achieved/abandoned); `cp_create_task` принимает `goal_id`, `origin`, `acceptance`, `evidence` |
| `cp_list_rules` / `cp_get_rule` | правила вывода работы (ADR-0063), read-only: триггер, условие, скилл интерпретации, действие и история оценок — откуда взялась задача с `origin.kind = rule` |
| `cp_process_get` / `cp_process_explain` | процесс по `key` или `key@version` с его версиями и объявленными сроками (`deadlines`); объяснение экземпляра — сроки и `slaState` процесса и открытых шагов (`sla`), решения по журналу с причинами, ответы памяти на `recall` и регламенты (`governedBy`) каждого решения |
| `cp_claim_task` / `cp_release_task` | захват/освобождение (явные действия) |
| `cp_start_run` / `cp_get_run` / `cp_get_run_context` | исполнение |
| `cp_checkpoint` | durable operational state для resume |
| `cp_record_action` | audit trail действий/skills |
| `cp_create_artifact` / `cp_list_artifacts` | результаты работы: ссылка, небольшой JSON или локальный файл (`file=`, байты загружаются в ядро, CP-ADR-0072) |
| `cp_get_artifact_content` | скачать содержимое артефакта — обычно входа текущей задачи (`for_task`) — в локальный файл; небольшой текст ещё и в ответе |
| `cp_request_approval` / `cp_list_approvals` / `cp_approve` / `cp_reject` | approvals (gate поддерживается) |
| `cp_suspend_run` | пауза на время ожидания approval |
| `cp_prepare_handoff` | атомарный checkpoint + suspend + release для смены harness |
| `cp_fail_run` / `cp_complete_run` | честная финализация |
| `cp_list_events` | replay событий с opaque-курсора |

Инструментов пакета здесь нет: проверка, тесты, план и применение пакета
процессов — у MCP-сервера автора из SDK `package-sdk` (`package-sdk mcp`,
плагин `package-author`; FR-002 фичи `package-sdk`, CP-ADR-0074 амендмент
К). Он читает каталог пакета с диска тем же кодом, что CLI `package-sdk`,
строит единый план и применяет его только по `planHash`. Здесь остаются
чтение опубликованного процесса (`cp_process_get`) и объяснение экземпляра
(`cp_process_explain`).

## Границы подтверждений человеком

`create/update`, relation mutations, `claim`, `handoff`, `complete`, `approve`,
`reject`, `release` — отдельные явные tools;
инструкции сервера предписывают Claude вызывать их только после явного
решения пользователя. Но это UX-слой: **сервер не доверяет** утверждению
«пользователь подтвердил» — авторизация и все инварианты проверяются на
Control Plane при каждом вызове.

## Рекомендуемый фрагмент CLAUDE.md проекта

```markdown
## Control Plane
This repository is connected to a Control Plane (MCP server `control-plane`).
- At the beginning of work, inspect the context (`cp_context` /
  `cp_get_context` for a specific task).
- Use Control Plane tasks as authoritative work items (`cp_list_work`).
- Before starting a selected task: `cp_claim_task`, then `cp_start_run`.
- Publish meaningful outputs as artifacts (`cp_create_artifact`).
- Checkpoint operational state before long waits (`cp_checkpoint`).
- Before changing harness, confirm and call `cp_prepare_handoff`; the next
  harness creates a new claim and run from server context.
- Record important findings/decisions with `cp_remember` so future sessions
  can recall them.
- Never complete a task without a valid active claim/fencing context; on
  `stale_claim` stop and consult the user.
```

CLAUDE.md — обучающий/политический слой, не реализация протокола; сервер
остаётся авторитетным. Credentials в CLAUDE.md не хранить.

## Восстановление после рестарта

Новый разговор Claude Code (без прошлого транскрипта): `cp_context`
показывает активные claims/runs/suspended runs; `cp_get_run_context` —
checkpoints и артефакты прошлых попыток; `cp_get_context` дополнительно
возвращает накопленные знания (findings/decisions прошлых сессий) из
внешнего Context Memory Engine, если он подключён. Если владение потеряно
(takeover) — инструменты вернут `stale_claim` c подсказкой остановиться и
обсудить с пользователем. Подробности — docs/harness-protocol.md §12.
