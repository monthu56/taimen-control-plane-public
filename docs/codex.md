# Codex + Control Plane

Codex подключается к product-neutral MCP adapter как human operator harness.
Project-scoped `.codex/config.toml`:

```toml
[mcp_servers.control_plane]
command = "control-plane-mcp"
required = true
default_tools_approval_mode = "prompt"

[mcp_servers.control_plane.env]
CONTROL_PLANE_HARNESS_TYPE = "codex"
CONTROL_PLANE_HARNESS_VERSION = "operator-template-v0.6"
CONTROL_PLANE_HARNESS_CLIENT_NAME = "codex-operator"
```

API key не помещают в TOML: `control-plane login` сохраняет его через
credential store. В начале разговора вызовите `cp_whoami` и `cp_context`,
покажите человеку Project focus, а до изменения target repository —
`cp_get_run_context`. Mutating tools требуют явного решения человека.

Для переключения покажите handoff summary/next steps/evidence человеку и
только после подтверждения вызовите `cp_prepare_handoff`. Новый Codex/Claude
процесс не продолжает старый Run: он выполняет новый claim, start Run и читает
server context. При `stale_claim` прекратите authoritative writes.

Не передавайте в Tasks, checkpoints и artifacts API keys, raw prompts, chat
history, hidden reasoning, terminal history или абсолютные filesystem paths.

