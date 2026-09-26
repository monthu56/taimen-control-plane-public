# control-plane-client

*English. Russian version: [README.ru.md](README.ru.md)*

The canonical Control Plane client (ADR-0030 of the superproject): the REST client
`ControlPlaneClient`, credential resolution (`resolve_credential`: an API key or
an IAM Platform Access Token from `~/.config/iam/credentials.json`, the Keychain or
the environment) and the exchange of a PAT for a short-lived access token of the required audience
(`IamCredential`). The only dependency is `httpx`.

It lives in the control-plane repository as a separate distribution so that consumers
(bidops, demos, harness plugins) can wire it in as the path dependency
`../control-plane/client` without the server dependencies of the core, and so that the client
version keeps pace with the server contract. control-plane itself wires it in the same way
(`[tool.uv.sources]` in the root `pyproject.toml`).

`IamCredential` takes the PAT from the local IAM store by the key
`issuer|tenant|principal`; a process whose PAT lives in its own file
(a container with a secret, the `<slug>.pat` of a vertical package) passes it explicitly —
`platform_access_token=` as a string or as a callable that is read on every
exchange (file rotation is picked up without a restart); the store is then not
touched, and the tenant may be empty. `ControlPlaneClient(..., user_agent=…)`
lets the consumer name itself in `User-Agent` ahead of the SDK token; commands
that the consumer retries based on its own state (`create_task`,
`succeed_run`, `fail_run`, `create_artifact`, `request_approval`) accept
`idempotency_key=` — otherwise the key is generated per call.

`control_plane_client.events` is the event consumer SDK (CP-ADR-0069):
`EventConsumer(client, types, workspace_id, cursor_store, handler, name=…)`
reads the journal through a subscription filter (type prefixes, a workspace
subtree), handles each event once in journal order and resumes where it
stopped — the cursor and the dedup record by `event.id` live in a
`CursorStore` (`MemoryCursorStore`, or `SqlAlchemyCursorStore` whose
transaction the handler can write into). A failed page is re-read after a
backoff; the WebSocket only wakes the consumer up, a 30-second poll covers
it. `to_cloudevent(event)` exports an event as CloudEvents 1.0. The extras
`[ws]`, `[sqlalchemy]` and `[events]` (both) add `websockets` and
`sqlalchemy`; the core client stays httpx-only. Guide:
[docs/events/consumer.md](../docs/events/consumer.md) (Russian), example:
[examples/approval_consumer.py](examples/approval_consumer.py).
