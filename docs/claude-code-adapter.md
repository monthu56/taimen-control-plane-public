# Claude Code как исполнитель: адаптер `control_plane_claude`

Реализация п. 1 [ADR-0016](../../docs/adr/ADR-0016-agent-runtime-and-execution-workspace.md)
для reference-демона `control-plane-agent`. Пакет —
`src/control_plane_claude/`.

## Что это

Адаптер под контракт `Adapter.execute(task, run, client, workspace)`. Цикл
координации — discovery, claim, run, рабочая копия, коммит-evidence,
завершение — принадлежит демону; адаптер отвечает на один вопрос внутри
цикла: «заставить Claude Code сделать работу». Доступа к базе у него нет,
`harness.type = "claude-code"` — значение существующей колонки, а не новая
сущность в ядре.

## Включение

```bash
CONTROL_PLANE_AGENT_ADAPTER=claude-code control-plane-agent
```

Импорт ленивый: демон продолжает работать на хосте, где Claude Code не
установлен, а протокольные тесты не тянут вендорский CLI.

| Переменная | Смысл |
|---|---|
| `CONTROL_PLANE_CLAUDE_BINARY` | путь к CLI, по умолчанию `claude` |
| `CONTROL_PLANE_CLAUDE_MODEL` | модель; пусто — как настроено у CLI |
| `CONTROL_PLANE_CLAUDE_PERMISSION_MODE` | `acceptEdits` по умолчанию |
| `CONTROL_PLANE_CLAUDE_TIMEOUT` | потолок одного turn в секундах (3600) |
| `CONTROL_PLANE_CLAUDE_MCP` | `0` — не пробрасывать MCP внутрь |
| `CONTROL_PLANE_CLAUDE_LOGS` | `0` — не писать локальный лог сессии |
| `CONTROL_PLANE_TRACE_TRANSCRIPT` / `…_ACTIONS` / `…_TOOL_RESULTS` | `0` — выключить публикацию транскрипта / actions на инструменты / вывод инструментов в транскрипте (ADR-0051) |
| `CONTROL_PLANE_CLAUDE_RESUME` | `0` — всегда начинать новую сессию |
| `CONTROL_PLANE_CONTEXT_BUDGET_CHARS` | бюджет раздела «Контекст задачи» в prompt, символов (12000); общий для Claude Code, Codex и OpenCode (ADR-0059) |
| `CONTROL_PLANE_CLAUDE_PROMPT_FILE` | файл соглашений репозитория — четвёртый слой инструкций после platform/project/taskType (CP-ADR-0066); читается на каждом запуске. У Codex и OpenCode — `CONTROL_PLANE_CODEX_PROMPT_FILE`, `CONTROL_PLANE_OPENCODE_PROMPT_FILE` |
| `CONTROL_PLANE_CLAUDE_RUNTIME_DIR` | где лежат mcp.json и логи (`~/.claude-runner`) |

Аутентификация — `CLAUDE_CODE_OAUTH_TOKEN` в окружении runner (подписка) или
`ANTHROPIC_API_KEY` (класс credential `api_key` по ADR-0016 п. 4). Адаптер
токен не читает и никуда не копирует: дочерний процесс наследует окружение.

## Три решения, которые легко сделать неправильно

**Промпт идёт через stdin.** Аргумент виден в таблице процессов любому на
хосте. По той же причине в argv не попадает ничего секретного, а MCP-серверы,
которые запускает CLI, получают credential по наследству окружения — не из
файла конфигурации, который был бы вторым местом утечки.

**Session id генерируется до запуска и сразу пишется в checkpoint.** Id,
известный только процессу, который затем умер, — это id, который никто не
продолжит. Порядок «checkpoint → процесс» проверяется тестом; следующая
попытка находит запись `claude-code.session` и передаёт `--resume`, вместо
того чтобы платить за разговор, забывший всё сделанное.

**Сырой поток остаётся на хосте, ограниченный транскрипт уходит.** stream-json
пишется в `<runtime>/sessions/<publicId>-<session>.jsonl` (`0600`, потолок
32 МБ). В Control Plane уходят два артефакта: `report` с финальным summary и
счётчиками (модель, ход, длительность, стоимость, токены) и `transcript` —
сообщения ассистента, вызовы инструментов с входом и результатом и итог,
отредактированные от путей хоста и credential'ов, не больше 512 КиБ
(ADR-0051). Промпт и блоки `thinking` не уходят никогда — последние только
считаются (`stats.hiddenReasoningBlocks`). Параллельно каждый `tool_use`
пишется run action'ом `tool.<имя>` со `started` и закрывается `:finish` по
`tool_result` — консоль показывает ход прогона, пока он идёт.

Управление: `CONTROL_PLANE_TRACE_TRANSCRIPT=0` — не публиковать документ,
`CONTROL_PLANE_TRACE_ACTIONS=0` — не писать actions на инструменты,
`CONTROL_PLANE_TRACE_TOOL_RESULTS=0` — оставить в документе только размер и
флаг ошибки у результатов инструментов.

## MCP внутрь агента (Q4, решено 2026-08-14)

MCP-сервер `control-plane` пробрасывается с `--strict-mcp-config`: агент сам
читает `cp_get_run_context`, пишет checkpoints и артефакты вместо пересказа
контекста в промпте — ровно то, для чего делался ADR-0046.

Авторитетные команды у агента отобраны: `--disallowedTools` заполняется
`withheld_tool_names()` из самого MCP-сервера — всё, что не помечено
read-only и не входит в `EVIDENCE_TOOLS`. Список **вычисляется, а не
переписывается**: копия имён в адаптере означала бы, что новый инструмент
сервера остаётся вызываемым, пока кто-то не вспомнит про второе место.
Инструмент без аннотации считается авторитетным — забытая разметка закрывает
дверь, а не открывает.

Это сужение задачи, **не граница безопасности**: агент работает под тем же
credential, что и адаптер, и может дойти до API мимо MCP. Настоящий потолок
даёт child grant, который считает сервер (ADR-0046).

## Границы

- Push и pull request не делаются: внешнее действие требует gate (AR-5).
- Исчерпание окна подписки сейчас — обычная ошибка turn и `fail_run`; штатным
  `checkpoint → suspend` это станет в AR-5.
- Семафор на общий с оператором credential pool — AR-4. До него параллельные
  прогоны выедают то же окно, что и интерактивные сессии человека.
- Один turn на run: адаптер не ведёт многоходовый диалог. Продолжение работы —
  это следующий run, читающий checkpoints предыдущего.
