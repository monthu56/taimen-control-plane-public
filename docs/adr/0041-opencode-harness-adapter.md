# ADR-0041: OpenCode как harness-адаптер поверх подтверждённого HTTP-контракта

Статус: Принято (v0.5)

## Контекст

Harness Protocol (ADR-0016) — семантический контракт поверх REST и журнала
событий. v0.4 поставила три клиента: MCP-сервер для Claude Code, CLI и
автономный agent-демон. Backlog требует адаптер OpenCode после закрытия
приоритетных platform foundations.

Требование прямое: использовать официальный контракт, не выдумывать его. Контракт
проверен по официальной документации OpenCode (`opencode serve`, OpenAPI 3.1 по
`GET /doc`): сервер по умолчанию `127.0.0.1:4096`, `GET /global/health`,
`POST /session`, `POST /session/{id}/message`, `GET /session/{id}/message`,
опциональный HTTP basic auth через `OPENCODE_SERVER_PASSWORD`.

## Решение

Пакет `control_plane_opencode` — harness-адаптер, а не переписывание core.

- Со стороны Control Plane используется существующий SDK `control_plane_client` и
  Harness Protocol v2: bootstrap context → discovery → claim → start run →
  checkpoint → complete/fail. Прямого доступа к базе нет.
- Со стороны OpenCode используется тонкий httpx-клиент по перечисленным выше
  путям. Пакет `opencode-ai` (pre-release) намеренно **не** добавлен в
  зависимости: контракт стабилен на уровне HTTP, а pre-release зависимость в
  production-сборке — лишний риск.
- Server-side continuity: id сессии OpenCode и id последнего сообщения пишутся в
  Run Checkpoint (`kind = "opencode.session"`). После рестарта адаптер читает
  чекпоинты нового Run и продолжает ту же сессию OpenCode вместо создания новой.
- Product-specific полей в core не добавляется: `harness.type = "opencode"` —
  значение существующей строковой колонки, `skills.protocol.opencode` уже
  существует в v0.2-словаре возможностей.
- Contract-тесты: обязательные — против локального ASGI-стенда, повторяющего
  документированные пути и формы (проверяют ровно то, что адаптер шлёт и как
  разбирает ответ); опциональные — против живого сервера, включаются
  `CP_TEST_OPENCODE_URL` по той же схеме gating, что и Memory contract tests.

## Последствия

- Адаптер полностью снаружи core: удаление пакета не влияет ни на один
  серверный инвариант.
- Форма `parts` в `POST /session/{id}/message` и структура ответа задокументированы
  укрупнённо; адаптер разбирает ответ защитно (текстовые части, отсутствующие
  поля — пустые значения) и не падает на расширении схемы.
- Живой прогон против реального `opencode serve` в CI не выполняется: бинарь не
  входит в образ. Это честно отмечено в известных ограничениях, а обязательный
  contract-тест против стенда закрывает регрессии формы запросов.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- file: src/control_plane_opencode/main.py
  repo: control-plane
- grep: {path: src/control_plane_opencode/main.py, pattern: 'CHECKPOINT_KIND = "opencode\.session"'}
  repo: control-plane
- grep: {path: pyproject.toml, pattern: 'control-plane-opencode = "control_plane_opencode\.main:main"'}
  repo: control-plane
- absent: {path: pyproject.toml, pattern: '"opencode-ai'}
  repo: control-plane
- grep: {path: tests/contract/test_opencode_contract.py, pattern: 'CP_TEST_OPENCODE_URL'}
  repo: control-plane
```
