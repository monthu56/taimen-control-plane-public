"""One prompt for every harness adapter (CP-ADR-0066 §5).

The Claude Code, Codex and OpenCode adapters each used to assemble their own
prompt: one read a repository conventions file, two pasted the project's raw
``effectiveConfig`` JSON instead, none knew how a task of a given type is to be
done. This module is the one place that turns what the Control Plane hands out
into the text an executor reads:

* the ``instructions`` block of the working context — layers the core
  assembled, general to specific: platform contract, project, task type — with
  its hash;
* the repository conventions file of this executor (``CONTROL_PLANE_*_PROMPT_FILE``),
  the fourth layer, which only the executor knows;
* the task itself and the project's status line;
* the memory pack, rendered by :mod:`control_plane_agent.context_pack` as data,
  not instructions (ADR-0059) — kept apart from the layers above.

Layers are written out in order under their own headings with their source;
nothing is merged. A missing block (an older server, an unavailable context)
leaves only the conventions: the task is authoritative and is in the prompt
regardless.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from control_plane_agent.context_pack import render_context_pack

logger = logging.getLogger("control_plane_agent.instructions")

HEADING = "## Instructions"
PREAMBLE = (
    "Layers from general to specific. A later layer adds to the earlier ones; "
    "none of them overrides the platform contract."
)
CONVENTIONS_TITLE = "Repository conventions"
_SOURCE_TITLES = {"platform": "Platform contract", "project": "Project", "taskType": "Task type"}


def prompt_file_from_environment(variable: str) -> Path | None:
    """The conventions file named by ``variable``, one variable per executor."""
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else None


def read_conventions(path: Path | None) -> str:
    """Read at execute time, so an operator can edit it without a restart."""
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("prompt file %s not readable: %s", path, exc)
        return ""


def _layer_title(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "")
    title = _SOURCE_TITLES.get(source, source or "Layer")
    ref = " ".join(str(item.get("ref") or "").split())
    version = item.get("version")
    label = f"{ref} v{version}" if ref and version is not None else ref
    return f"### {title} ({label})" if label else f"### {title}"


def render_instructions(block: Any, conventions: str = "") -> str:
    """The instructions section: server layers, then the conventions file."""
    layers = block.get("layers") if isinstance(block, dict) else None
    parts: list[str] = []
    for item in layers if isinstance(layers, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts += [_layer_title(item), "", text, ""]
    if conventions:
        parts += [f"### {CONVENTIONS_TITLE}", "", conventions, ""]
    if not parts:
        return ""
    lines = [HEADING, "", PREAMBLE, "", *parts]
    digest = block.get("hash") if isinstance(block, dict) else None
    if isinstance(digest, str) and digest:
        lines.append(f"Instructions hash: {digest}")
    return "\n".join(lines).rstrip()


def build_prompt(
    task: dict[str, Any],
    context: dict[str, Any],
    *,
    harness_note: str = "",
    conventions: str = "",
) -> str:
    """The whole prompt of one turn, identical in shape for every adapter.

    ``harness_note`` is what only this harness can say about its environment
    (whether Control Plane tools are available, where to work, what is
    recorded); everything else comes from the Control Plane or the task.
    """
    public_id = task.get("publicId") or task["id"]
    lines: list[str] = []
    if harness_note:
        lines += [harness_note.rstrip(), ""]
    section = render_instructions(context.get("instructions"), conventions)
    if section:
        lines += [section, ""]
    lines += [
        f"# Task {public_id}: {task.get('title', '')}",
        "",
        str(task.get("description") or "(no description)"),
    ]
    project = (context.get("operational") or {}).get("project")
    if isinstance(project, dict) and project:
        lines += [
            "",
            "## Project",
            f"- status: {project.get('statusKey')} ({project.get('systemStatusCategory')})",
            f"- template: {project.get('templateKey')} v{project.get('templateVersion')}",
        ]
    lines += ["", render_context_pack(context)]
    return "\n".join(lines)
