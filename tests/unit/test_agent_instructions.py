"""Executor instructions: layers, hash, validation and the shared render (CP-ADR-0066)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from control_plane.domain.agent_instructions import (
    MAX_INSTRUCTIONS_BYTES,
    PLATFORM_CONTRACT,
    PLATFORM_CONTRACT_REF,
    PLATFORM_CONTRACT_VERSION,
    assemble_instructions,
    instruction_refs,
    instructions_hash,
    layer,
    validate_instructions,
)
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import validate_config_document
from control_plane_agent.instructions import (
    HEADING,
    build_prompt,
    read_conventions,
    render_instructions,
)
from control_plane_claude.adapter import ClaudeCodeAdapter
from control_plane_claude.cli import ClaudeCodeCLI
from control_plane_codex.adapter import CodexAdapter
from control_plane_codex.cli import CodexCLI
from control_plane_opencode.main import OpenCodeAdapter

PROJECT = layer("project", "project:p-1", 3, "Конституция проекта: тесты до кода.")
TASK_TYPE = layer("taskType", "taskType:sdd.spec", 2, "Пиши спецификацию, не код.")

# -- the core ---------------------------------------------------------------------


def test_layers_go_from_general_to_specific() -> None:
    block = assemble_instructions(project=PROJECT, task_type=TASK_TYPE)
    assert [item["source"] for item in block["layers"]] == ["platform", "project", "taskType"]
    platform = block["layers"][0]
    assert platform == {
        "source": "platform",
        "ref": PLATFORM_CONTRACT_REF,
        "version": PLATFORM_CONTRACT_VERSION,
        "text": PLATFORM_CONTRACT,
    }
    assert block["hash"] == instructions_hash(block["layers"])
    assert block["hash"].startswith("sha256:") and len(block["hash"]) == 7 + 64


def test_empty_layers_are_left_out() -> None:
    block = assemble_instructions(project=layer("project", "project:p", 1, ""), task_type=None)
    assert [item["source"] for item in block["layers"]] == ["platform"]
    assert block == assemble_instructions()


def test_hash_follows_text_version_and_order() -> None:
    base = assemble_instructions(project=PROJECT, task_type=TASK_TYPE)["hash"]
    edited = layer("taskType", "taskType:sdd.spec", 3, "Пиши спецификацию и план.")
    assert assemble_instructions(project=PROJECT, task_type=edited)["hash"] != base
    assert assemble_instructions(project=None, task_type=TASK_TYPE)["hash"] != base
    # Deterministic: the same layers always give the same hash.
    assert assemble_instructions(project=PROJECT, task_type=TASK_TYPE)["hash"] == base
    # NFC: the same text in two Unicode forms is the same text.
    composed = layer("taskType", "t", 1, "é")
    decomposed = layer("taskType", "t", 1, "é")
    assert instructions_hash([composed]) == instructions_hash([decomposed])


def test_refs_carry_versions_not_text() -> None:
    block = assemble_instructions(project=PROJECT, task_type=TASK_TYPE)
    refs = instruction_refs(block)
    assert refs["hash"] == block["hash"]
    assert refs["layers"][1:] == [
        {"source": "project", "ref": "project:p-1", "version": 3},
        {"source": "taskType", "ref": "taskType:sdd.spec", "version": 2},
    ]


def test_validation_size_type_and_secrets() -> None:
    assert validate_instructions(None, field="instructions") == ""
    assert validate_instructions("Rotate the API key before Friday.", field="i") != ""
    assert validate_instructions("x" * MAX_INSTRUCTIONS_BYTES, field="i")
    with pytest.raises(ValidationError) as too_large:
        validate_instructions("я" * (MAX_INSTRUCTIONS_BYTES // 2 + 1), field="instructions")
    assert too_large.value.code == "instructions_too_large"
    with pytest.raises(ValidationError) as wrong_type:
        validate_instructions({"text": "x"}, field="instructions")
    assert wrong_type.value.code == "invalid_instructions"
    with pytest.raises(ValidationError) as secret:
        validate_instructions("deploy with token=AKIAABCDEFGHIJKLMNOP", field="instructions")
    assert secret.value.code == "secret_material_rejected"
    assert secret.value.details["field"] == "instructions"


def test_project_layer_is_checked_in_the_config_document() -> None:
    validate_config_document({"settings": {"agentInstructions": "Пиши тесты."}})
    with pytest.raises(ValidationError) as secret:
        validate_config_document(
            {"settings": {"agentInstructions": "-----BEGIN RSA PRIVATE KEY-----"}}
        )
    assert secret.value.code == "secret_material_rejected"
    assert secret.value.details["field"] == "config.settings.agentInstructions"
    with pytest.raises(ValidationError) as too_large:
        validate_config_document(
            {"settings": {"agentInstructions": "x" * (MAX_INSTRUCTIONS_BYTES + 1)}},
            field_name="settings",
        )
    assert too_large.value.code == "instructions_too_large"


# -- the shared render ------------------------------------------------------------

TASK = {"id": "t-1", "publicId": "TASK-1", "title": "Do it", "description": "Details."}
BLOCK = assemble_instructions(project=PROJECT, task_type=TASK_TYPE)


def test_render_writes_layers_in_order_with_sources_and_hash() -> None:
    text = render_instructions(BLOCK, "uv.lock не коммитить.")
    assert text.startswith(HEADING)
    positions = [
        text.index(f"### Platform contract ({PLATFORM_CONTRACT_REF} v{PLATFORM_CONTRACT_VERSION})"),
        text.index("### Project (project:p-1 v3)"),
        text.index("### Task type (taskType:sdd.spec v2)"),
        text.index("### Repository conventions"),
    ]
    assert positions == sorted(positions)
    assert "Пиши спецификацию, не код." in text
    assert text.endswith(f"Instructions hash: {BLOCK['hash']}")


def test_render_without_block_keeps_only_conventions() -> None:
    assert render_instructions(None) == ""
    assert render_instructions({"layers": "junk"}) == ""
    text = render_instructions(None, "conventions")
    assert "### Repository conventions" in text and "Instructions hash" not in text


def test_conventions_file_is_optional(tmp_path: Path) -> None:
    assert read_conventions(None) == ""
    assert read_conventions(tmp_path / "missing.md") == ""
    path = tmp_path / "c.md"
    path.write_text("  rules \n")
    assert read_conventions(path) == "rules"


def _claude(context: dict[str, Any], prompt_file: Path | None) -> str:
    adapter = ClaudeCodeAdapter(ClaudeCodeCLI(binary="claude"), prompt_file=prompt_file)
    return adapter._build_prompt(TASK, context)


def _codex(context: dict[str, Any], prompt_file: Path | None) -> str:
    adapter = CodexAdapter(CodexCLI(binary="codex"), prompt_file=prompt_file)
    return adapter._build_prompt(TASK, context)


def _opencode(context: dict[str, Any], prompt_file: Path | None) -> str:
    adapter = OpenCodeAdapter.__new__(OpenCodeAdapter)  # the prompt needs no connections
    adapter.prompt_file = prompt_file
    return adapter._build_prompt(TASK, context)


CONTEXT: dict[str, Any] = {
    "instructions": BLOCK,
    "operational": {
        "project": {
            "statusKey": "active",
            "systemStatusCategory": "active",
            "templateKey": "software",
            "templateVersion": 1,
            "effectiveConfig": {"settings": {"marker": "RAW-CONFIG-MARKER"}},
        }
    },
    "memoryStatus": "disabled",
}


def test_render_is_the_same_for_the_three_adapters(tmp_path: Path) -> None:
    conventions = tmp_path / "conventions.md"
    conventions.write_text("uv.lock не коммитить.\n")
    prompts = {
        name: build(CONTEXT, conventions)
        for name, build in (("claude", _claude), ("codex", _codex), ("opencode", _opencode))
    }
    expected = build_prompt(TASK, CONTEXT, conventions="uv.lock не коммитить.")
    for name, prompt in prompts.items():
        # Only the harness note differs: what only this harness can say.
        assert prompt.endswith(expected), name
        assert "RAW-CONFIG-MARKER" not in prompt, name
        assert "effectiveConfig" not in prompt and "Effective configuration" not in prompt
        assert "Пиши спецификацию, не код." in prompt
        assert "uv.lock не коммитить." in prompt


def test_type_without_instructions_keeps_the_prompt_shape() -> None:
    """Without project and type layers the prompt is what it was before, plus the
    platform contract in place of the protocol part of the harness note."""
    context = {**CONTEXT, "instructions": assemble_instructions()}
    prompt = build_prompt(TASK, context)
    assert "### Platform contract" in prompt
    assert "### Project (" not in prompt and "### Task type" not in prompt
    for section in ("# Task TASK-1: Do it", "Details.", "## Project", "- status: active"):
        assert section in prompt
    assert prompt.index("### Platform contract") < prompt.index("# Task TASK-1")
    assert "RAW-CONFIG-MARKER" not in prompt
