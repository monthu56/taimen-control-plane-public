*Русская версия. English: [README.md](README.md)*

# control-plane-client

Канонический клиент Control Plane (ADR-0030 суперпроекта): REST-клиент
`ControlPlaneClient`, резолюция credential (`resolve_credential`: API key или
IAM Platform Access Token из `~/.config/iam/credentials.json`, Keychain или
окружения) и обмен PAT на короткоживущий access token нужного audience
(`IamCredential`). Зависимость — только `httpx`.

Живёт в репозитории control-plane отдельным дистрибутивом, чтобы потребители
(bidops, демо, harness-плагины) подключали его path-зависимостью
`../control-plane/client` без серверных зависимостей ядра, а версия клиента
шла в ногу с контрактом сервера. Сам control-plane подключает его так же
(`[tool.uv.sources]` в корневом `pyproject.toml`).

`IamCredential` берёт PAT из локального хранилища IAM по ключу
`issuer|tenant|principal`; процесс, у которого PAT живёт в собственном файле
(контейнер с секретом, `<slug>.pat` вертикального пакета), передаёт его явно —
`platform_access_token=` строкой или callable, который читается при каждом
обмене (ротация файла подхватывается без перезапуска); хранилище тогда не
трогается, а tenant может быть пустым. `ControlPlaneClient(..., user_agent=…)`
даёт потребителю назвать себя в `User-Agent` перед токеном SDK; команды,
которые потребитель повторяет по собственному состоянию (`create_task`,
`succeed_run`, `fail_run`, `create_artifact`, `request_approval`), принимают
`idempotency_key=` — иначе ключ генерируется на вызов.

`control_plane_client.events` — SDK потребителя событий (CP-ADR-0069):
`EventConsumer(client, types, workspace_id, cursor_store, handler, name=…)`
читает журнал через фильтр подписки (префиксы типа, поддерево workspace),
обрабатывает каждое событие один раз в порядке журнала и продолжает с места
остановки — курсор и отметки обработанных `event.id` хранит `CursorStore`
(`MemoryCursorStore` или `SqlAlchemyCursorStore`, в транзакцию которого может
писать обработчик). Упавшая страница читается заново после паузы; WebSocket
только будит потребителя, страхует опрос раз в 30 с. `to_cloudevent(event)` —
экспорт события в CloudEvents 1.0. Экстры `[ws]`, `[sqlalchemy]` и `[events]`
(оба) добавляют `websockets` и `sqlalchemy`; ядро клиента остаётся только на
`httpx`. Руководство — [docs/events/consumer.md](../docs/events/consumer.md),
пример — [examples/approval_consumer.py](examples/approval_consumer.py).
