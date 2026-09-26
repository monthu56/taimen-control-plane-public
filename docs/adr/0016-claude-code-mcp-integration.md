# ADR-0016. Harness Protocol поверх REST + событий; Claude Code через MCP

Статус: принято (2026-08-11, v0.3)

## Контекст

Нужен единый протокол подключения рабочих сред (человеческих и автономных) и
конкретная интеграция первого human harness — Claude Code. Претенденты на
транспорт: новый wire-протокол (gRPC/WS-RPC) или существующий REST + журнал
событий. Претенденты на интеграцию Claude Code: MCP server, plugin/hooks,
CLI-обёртка, патчи самого Claude Code.

## Решение

1. **Harness Protocol = семантический контракт поверх существующего REST**
   (+ `GET /events` / WebSocket для потока). Версия — строка `taimen-harness/1`,
   передаётся при open-session (`harness.protocolVersion: "1"`); негодная
   версия — `422 unsupported_protocol_version` со списком поддерживаемых.
   Protocol capabilities клиента — декларация (`events.realtime`,
   `tasks.interactive`, `skills.protocol.*`, ...); неизвестные отбрасываются
   молча (forward compatibility).
2. **Claude Code подключается через MCP** — официальный, минимально
   инвазивный механизм расширения: stdio-сервер `taimen-mcp` (официальный
   Python MCP SDK) поверх `taimen_client`. Никаких патчей Claude Code и
   недокументированных internals. MCP-tools отражают user workflow
   (discover → inspect → claim → run → artifact → complete), а не таблицы.

## Обоснование

Существующий REST уже несёт все инварианты (транзакции, идемпотентность,
ETag, fencing); новый транспорт добавил бы поверхность отказов, не добавив
семантики. MCP — поддерживаемый способ дать Claude Code инструменты с
контролируемыми границами; сервер-адаптер stateless, поэтому доверие к нему
не требуется: Control Plane перепроверяет всё.

## Последствия

- Версия протокола эволюционирует редко и явно; `1` покрывает v0.3.
- MCP-сервер держит только кэш «над чем работает это окно» (session/claim/run
  id) — восстановимо через `taimen_context`; авторитетного состояния в нём нет.
- Любой будущий harness (Taimen Desktop, OpenCode-daemon, CI) использует тот
  же REST-контракт — MCP лишь один из адаптеров.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/domain/enums.py, pattern: 'SUPPORTED_HARNESS_PROTOCOL_VERSIONS = frozenset'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/sessions.py, pattern: '"unsupported_protocol_version"'}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: '^from mcp\.server import MCPServer'}
  repo: control-plane
- grep: {path: pyproject.toml, pattern: 'control-plane-mcp = "control_plane_mcp\.server:main"'}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: 'async def cp_claim_task\('}
  repo: control-plane
```
