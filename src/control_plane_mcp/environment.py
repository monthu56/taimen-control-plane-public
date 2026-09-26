"""What a harness adapter tells the MCP server it starts for an agent.

A runner that drives an agent (Claude Code) holds the claim and the run; the
MCP server the agent starts is a separate process that would otherwise know
neither, and whatever the agent records — an artifact, a checkpoint, an
action — would land on no run. The adapter passes the ids through the
environment the agent inherits; the server adopts them as its working state.
Ids only: the claim, its fencing token and the lifecycle stay with the runner.
"""

MCP_TASK_ENV = "CONTROL_PLANE_TASK"
MCP_RUN_ENV = "CONTROL_PLANE_RUN_ID"
