# ADR-0040: Закрытие compatibility window продуктового кодового имени

Статус: Принято (v0.5)

## Контекст

ADR-0022 нейтрализовал публичные имена в v0.4 и оставил окно совместимости:
legacy env-переменные, legacy storage-локации, пакет-шим, console-script алиасы,
алиасы классов SDK и объявление `legacyNames` в bootstrap. Там же было записано,
что удаление откладывается на будущую версию. Roadmap и baseline v0.4 называют
это обязательным пунктом reliability backlog для v0.5.

Инвентаризация перед удалением (79 упоминаний в 17 файлах) показала, что все
активные точки входа классифицируются и удаляются без потери функциональности,
а историческая часть (ADR-0016, ADR-0017, ADR-0022, миграции) переписываться не
должна.

## Решение

v0.5 — граница удаления. Удаляются активные точки входа:

| Что | Замена |
|---|---|
| пакет-шим `taimen_client` (+ подмодули) | `control_plane_client` |
| алиасы `TaimenClient`, `TaimenError` | `ControlPlaneClient`, `ControlPlaneError` |
| console-scripts `taimen`, `taimen-mcp`, `taimen-agent` | `control-plane*` |
| env `TAIMEN_API_KEY`, `TAIMEN_SERVER`, `TAIMEN_NO_KEYCHAIN`, `TAIMEN_AGENT_ADAPTER`, `TAIMEN_AGENT_WORKSPACE`, `TAIMEN_AGENT_POLL` | `CONTROL_PLANE_*` |
| keychain `ai.taimen.api-key`, `~/.config/taimen/credentials.json`, `.taimen/config.json` | `control-plane.api-key`, `~/.config/control-plane/`, `.control-plane/` |
| `LEGACY_HARNESS_PROTOCOL_NAMES` и поле `protocol.legacyNames` | `control-harness` |

Тихого отказа быть не должно. Клиентские точки входа (CLI, MCP, agent, SDK
`resolve_api_key`) при обнаружении установленной legacy-переменной или
существующей legacy-локации печатают явную ошибку миграции с точным именем
замены — `ControlPlaneError("legacy_configuration", ...)`. Диагностика
проверяется тестами.

Сохраняются без изменений:

- ADR-0016, ADR-0017, ADR-0022 и все миграции — неизменяемая история;
- совместимость **протокольная**, не продуктовая: `protocolVersion: "1"`,
  legacy integer `?after=<sequence>` и v0.3-кодировка `nextCursor` остаются
  принятыми входами (это ADR-0024, а не кодовое имя).

Branding guard усилен: allowlist сокращается до исторических ADR и самого теста;
добавлена проверка, что удалённые точки входа действительно отсутствуют
(`pyproject.toml` не содержит legacy scripts, пакет-шим не импортируется,
legacy env не читаются).

## Последствия

- Клиент v0.3/v0.4, полагающийся на кодовое имя, перестаёт работать с явной
  ошибкой, а не молча. Это ожидаемое breaking-изменение публичного контракта
  клиентских инструментов; серверный HTTP API не затронут.
- Пользователь, у которого ключ лежит только в legacy-локации, получает
  инструкцию перевыпустить/перелогиниться, а не пустой ответ.
- Ни один тест больше не «проверяет legacy-поведение»: тесты проверяют, что
  legacy-поведения нет и что диагностика понятна.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- absent: {path: pyproject.toml, pattern: '^taimen'}
  repo: control-plane
- absent: {path: "**/*.py", pattern: '^\s*(from|import) taimen_client'}
  repo: control-plane
- absent: {path: "src/**/*.py", pattern: 'LEGACY_HARNESS_PROTOCOL_NAMES'}
  repo: control-plane
- grep: {path: client/src/control_plane_client/credentials.py, pattern: '"TAIMEN_API_KEY": "CONTROL_PLANE_API_KEY"'}
  repo: control-plane
- grep: {path: src/control_plane_cli/main.py, pattern: 'removed_environment_variables\(\)'}
  repo: control-plane
- grep: {path: tests/unit/test_branding.py, pattern: 'def test_removed_entry_points_are_gone'}
  repo: control-plane
```
