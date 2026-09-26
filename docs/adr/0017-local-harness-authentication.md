# ADR-0017. Аутентификация локального harness: API-ключи + локальный credential store

Статус: принято (2026-08-11, v0.3)

## Контекст

Claude Code / desktop-harness работают на машине пользователя. Long-lived
plaintext-ключ в репозитории, CLAUDE.md, MCP-конфиге или истории shell
недопустим. Полноценный OAuth-провайдер — избыточен для v0.3.

## Решение

Остаёмся на существующей API-key архитектуре (ADR-0006), добавляя клиентскую
дисциплину хранения:

- Резолюция ключа (taimen_client.credentials): `TAIMEN_API_KEY` env →
  macOS Keychain (`security`, сервис `ai.taimen.api-key`, account = server
  URL) → `~/.config/taimen/credentials.json` (chmod 0600).
- `taimen login` валидирует ключ запросом контекста и пишет в самое
  безопасное доступное хранилище; `taimen logout` удаляет запись.
- Привязка проекта — `.taimen/config.json`: только несекретные метаданные
  (server/tenant/workspace/repository), можно коммитить; секретные локальные
  файлы — под `.taimen/*.local.json` в .gitignore.
- MCP-конфиг Claude Code не содержит ключа: `taimen-mcp` резолвит его сам.

## Обоснование

Ключи уже revocable server-side, хешированы at rest, идентифицируют principal
— требования v0.3 закрываются без новой auth-подсистемы. Device-flow /
short-lived tokens остаются кандидатами на будущее, когда появится
интерактивный identity provider.

## Последствия

- Ключ не попадает в repo/логи/события (логирование ключей уже запрещено
  redaction'ом v0.1).
- Компрометация машины пользователя = компрометация ключа — как и у любого
  локального credential; лечится revoke.
- На не-macOS платформах используется файл с правами 0600.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

Имена `taimen_*` из текста ADR переименованы ADR-0022/0040; пробы проверяют ту же дисциплину на канонических именах.

```conformance
- grep: {path: client/src/control_plane_client/credentials.py, pattern: '_ENV_VAR = "CONTROL_PLANE_API_KEY"'}
  repo: control-plane
- grep: {path: client/src/control_plane_client/credentials.py, pattern: '_KEYCHAIN_SERVICE = "control-plane\.api-key"'}
  repo: control-plane
- grep: {path: client/src/control_plane_client/credentials.py, pattern: 'os\.O_WRONLY \| os\.O_CREAT \| os\.O_TRUNC, 0o600'}
  repo: control-plane
- grep: {path: client/src/control_plane_client/config.py, pattern: 'CONFIG_DIR = "\.control-plane"'}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: 'credential = resolve_credential\(server\)'}
  repo: control-plane
```
