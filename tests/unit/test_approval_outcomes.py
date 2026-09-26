"""The approval_schema grammar (CP-ADR-0061): closed actions, closed paths."""

from typing import Any

import pytest

from control_plane.application.commands.approval_outcomes import _basis_revoked
from control_plane.domain.approval_outcomes import (
    Path,
    parse_approval_schema,
    parse_path,
    render,
    skill_calls,
)
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import SkillInvocation

CONTEXT: dict[str, Any] = {
    ("task", "publicId"): "TASK-000002",
    ("approval", "comment"): "Fix the null check",
    ("approval", "decidedBy"): None,
    ("spawnedBy", "publicId"): "TASK-000001",
    ("spawnedBy", "artifact:commit:branch"): "task/TASK-000001",
    ("spawnedBy", "description"): "x" * 50,
    ("task", "customFields.branch"): "task/TASK-000001",
}


def _resolve(path: Path) -> Any:
    if path.artifact_type is not None:
        return CONTEXT.get((path.root, f"artifact:{path.artifact_type}:{path.metadata_key}"))
    if path.key is not None:
        return CONTEXT.get((path.root, f"{path.field}.{path.key}"))
    return CONTEXT.get((path.root, path.field))


@pytest.mark.parametrize(
    "expression",
    [
        "$.task.publicId",
        "$.approval.decidedAt",
        "$.spawnedBy.assigneeId",
        "$.spawnedBy.artifact[commit].metadata.branch",
        "$.task.artifact[github.pull_request].metadata.number",
        "$.task.customFields.repository",
        "$.spawnedBy.customFields.targetBranch!",
        "$.spawnedBy.description|truncate:4000",
        "$.spawnedBy.description!|truncate:10",
    ],
)
def test_supported_paths_parse(expression: str) -> None:
    assert parse_path(expression).text == expression


@pytest.mark.parametrize(
    "expression",
    [
        "$.tenant.id",
        "$.task",
        "$.task.createdBy",
        "$.approval.artifact[commit].metadata.branch",
        "$.spawnedBy.artifact[commit]",
        "$.spawnedBy.artifact.commit.metadata.branch",
        "$.task.publicId.length",
        "$.approval.customFields.x",
        "$.task.customFields",
        "$.task.customFields.a.b",
        "$.task.description|truncate:0",
        # Only inside the onFailure of an invokeSkill.
        "$.invocation.output.merged",
    ],
)
def test_everything_else_is_refused(expression: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_path(expression)
    assert caught.value.code == "invalid_approval_schema"


def test_a_whole_expression_keeps_the_raw_value() -> None:
    assert render("$.approval.decidedBy", _resolve) is None
    assert render("$.task.publicId", _resolve) == "TASK-000002"


def test_a_template_substitutes_text_and_blanks_missing_values() -> None:
    rendered = render(
        "Fixes for $.spawnedBy.publicId on $.spawnedBy.artifact[commit].metadata.branch "
        "by $.approval.decidedBy.",
        _resolve,
    )
    assert rendered == "Fixes for TASK-000001 on task/TASK-000001 by ."


def test_rendering_walks_objects_and_leaves_other_values_alone() -> None:
    assert render({"spawned_by": "$.task.publicId", "n": 3, "on": True}, _resolve) == {
        "spawned_by": "TASK-000002",
        "n": 3,
        "on": True,
    }


def test_text_that_merely_looks_like_code_is_just_text() -> None:
    """No calls: ``()`` after a path is literal text around a plain value."""
    assert render("$.task.publicId()", _resolve) == "TASK-000002()"


def test_empty_document_declares_nothing() -> None:
    schema = parse_approval_schema({})
    assert schema.empty
    assert schema.actions_for("default", "approved") == ()


def test_actions_keep_their_order_per_outcome() -> None:
    schema = parse_approval_schema(
        {
            "gates": {
                "default": {
                    "outcomes": {
                        "rejected": [
                            {"comment": {"body": "$.approval.comment"}},
                            {"transition": {"status": "blocked"}},
                            {"completeTask": {}},
                        ]
                    }
                }
            }
        },
        statuses=frozenset({"todo", "blocked", "done"}),
    )
    assert [a.name for a in schema.actions_for("default", "rejected")] == [
        "comment",
        "transition",
        "completeTask",
    ]
    assert schema.actions_for("default", "approved") == ()


def test_transition_of_another_task_is_checked_at_runtime_only() -> None:
    parse_approval_schema(
        {
            "gates": {
                "default": {
                    "outcomes": {
                        "approved": [
                            {"transition": {"status": "shipped", "task": "$.spawnedBy.id"}}
                        ]
                    }
                }
            }
        },
        statuses=frozenset({"todo", "done"}),
    )


def test_secret_looking_inputs_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_approval_schema(
            {
                "gates": {
                    "default": {
                        "outcomes": {
                            "approved": [
                                {"invokeSkill": {"skill": "git.merge", "inputs": {"token": "x"}}}
                            ]
                        }
                    }
                }
            }
        )
    assert caught.value.code == "secret_material_rejected"


@pytest.mark.parametrize(
    "value",
    [
        "$.approval.decidedBy!",  # whole expression, None
        "Review by $.approval.decidedBy!",  # inside a template
        "$.spawnedBy.artifact[pull_request].metadata.number!",
    ],
)
def test_a_required_expression_that_resolves_to_nothing_is_refused(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        render({"input": value}, _resolve)
    assert caught.value.code == "unresolved_expression"
    assert caught.value.details["expression"].endswith("!")


def test_a_required_expression_with_a_value_renders_like_any_other() -> None:
    assert render("$.task.publicId!", _resolve) == "TASK-000002"
    assert (
        render("Branch $.spawnedBy.artifact[commit].metadata.branch!: go", _resolve)
        == "Branch task/TASK-000001: go"
    )
    assert parse_path("$.task.id!").required
    assert not parse_path("$.task.id").required


def test_named_gates_are_refused_until_approvals_carry_a_gate_name() -> None:
    with pytest.raises(ValidationError, match="only the 'default' gate"):
        parse_approval_schema({"gates": {"release": {"outcomes": {"approved": []}}}})


def _approved(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"gates": {"default": {"outcomes": {"approved": list(actions)}}}}


def test_truncate_caps_text_and_keeps_short_values() -> None:
    assert render("$.spawnedBy.description|truncate:10", _resolve) == "x" * 9 + "…"
    assert render("$.spawnedBy.description|truncate:50", _resolve) == "x" * 50
    assert render("[$.spawnedBy.description|truncate:3]", _resolve) == "[xx…]"
    # Text around an expression that merely contains a pipe stays text.
    assert render("$.task.publicId|done", _resolve) == "TASK-000002|done"


def test_custom_fields_resolve_by_key() -> None:
    assert render("$.task.customFields.branch!", _resolve) == "task/TASK-000001"
    with pytest.raises(ValidationError) as caught:
        render("$.task.customFields.repository!", _resolve)
    assert caught.value.code == "unresolved_expression"


def test_invoke_skill_takes_a_pinned_reference_and_inputs() -> None:
    schema = parse_approval_schema(
        _approved(
            {
                "invokeSkill": {
                    "skill": "git.merge@1",
                    "inputs": {"branch": "$.task.customFields.branch!"},
                    "onSuccess": [{"completeTask": {}}],
                }
            },
        )
    )
    invoke, complete = schema.actions_for("default", "approved")
    assert invoke.inputs == {
        "skill": "git.merge@1",
        "inputs": {"branch": "$.task.customFields.branch!"},
    }
    assert (complete.name, complete.reacts_to, complete.when) == ("completeTask", 0, "onSuccess")


def test_reactions_follow_the_outcome_with_a_stable_index() -> None:
    """Reactions run after every main action: they wait on the skill, the rest does not."""
    schema = parse_approval_schema(
        _approved(
            {
                "invokeSkill": {
                    "skill": "git.merge@1",
                    "expect": {"merged": True},
                    "onFailure": [
                        {"comment": {"body": "Merge failed: $.invocation.output.reason"}},
                        {"comment": {"body": "$.invocation.error.message"}},
                    ],
                    "onSuccess": [{"comment": {"body": "Merged $.invocation.output.sha"}}],
                }
            },
            {"comment": {"body": "Merge queued"}},
        ),
        terminal=frozenset({"done"}),
    )
    actions = schema.actions_for("default", "approved")
    assert [(a.name, a.reacts_to, a.when) for a in actions] == [
        ("invokeSkill", None, None),
        ("comment", None, None),
        ("comment", 0, "onSuccess"),
        ("comment", 0, "onFailure"),
        ("comment", 0, "onFailure"),
    ]
    assert "onFailure" not in actions[0].inputs
    assert "onSuccess" not in actions[0].inputs
    assert actions[0].inputs["expect"] == {"merged": True}


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        # The approval stops being an external_write basis once its task closes.
        (_approved({"completeTask": {}}, {"invokeSkill": {"skill": "x"}}), "must come before"),
        (
            _approved({"completeTask": {"task": "$.task.id!"}}, {"invokeSkill": {"skill": "x"}}),
            "must come before",
        ),
        (_approved({"transition": {"status": "done"}}, {"invokeSkill": {"skill": "x"}}), "before"),
        # ...and closing it right after queueing the call would close it before
        # the call is claimed (basis_revoked / task_terminal): only in a reaction.
        (_approved({"invokeSkill": {"skill": "x"}}, {"completeTask": {}}), "may not have run"),
        (
            _approved({"invokeSkill": {"skill": "x"}}, {"completeTask": {"task": "$.task.id"}}),
            "may not have run",
        ),
        (
            _approved({"invokeSkill": {"skill": "x"}}, {"transition": {"status": "done"}}),
            "may not have run",
        ),
        # Whether a transition closes the gated task must be decidable now.
        (_approved({"transition": {"status": "$.approval.comment"}}), "literal status"),
        (
            _approved({"transition": {"task": "$.task.id!", "status": "$.task.status"}}),
            "literal status",
        ),
        (
            _approved(
                {"invokeSkill": {"skill": "x", "onFailure": [{"invokeSkill": {"skill": "y"}}]}}
            ),
            "cannot invoke another skill",
        ),
        (
            _approved(
                {"invokeSkill": {"skill": "x", "onSuccess": [{"invokeSkill": {"skill": "y"}}]}}
            ),
            "cannot invoke another skill",
        ),
        (_approved({"invokeSkill": {"skill": "x", "onSuccess": {}}}), "list of actions"),
        (_approved({"invokeSkill": {"skill": "x", "onFailure": {}}}), "list of actions"),
        (_approved({"invokeSkill": {"skill": "x", "expect": {}}}), "non-empty object"),
        (_approved({"invokeSkill": {"skill": "x", "expect": {"a": [1]}}}), "only strings"),
        (
            _approved({"invokeSkill": {"skill": "x", "expect": {"a": "$.task.id"}}}),
            "literals",
        ),
        (_approved({"invokeSkill": {"skill": "x", "input": {}}}), "unknown inputs"),
        (_approved({"invokeSkill": {"skill": "git.merge@"}}), "skill reference"),
        (_approved({"invokeSkill": {"skill": "Git.Merge"}}), "skill reference"),
        (_approved({"comment": {"body": "$.invocation.status"}}), "unknown root"),
    ],
)
def test_invalid_invoke_skill_declarations_are_refused(
    document: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_approval_schema(
            document,
            statuses=frozenset({"todo", "done"}),
            terminal=frozenset({"done"}),
        )
    assert caught.value.code == "invalid_approval_schema"
    assert fragment in caught.value.message


def test_closing_another_task_first_does_not_block_invoke_skill() -> None:
    parse_approval_schema(
        _approved(
            {"completeTask": {"task": "$.spawnedBy.id"}},
            {"invokeSkill": {"skill": "x", "onSuccess": [{"completeTask": {}}]}},
            {"transition": {"task": "$.spawnedBy.id", "status": "$.approval.comment"}},
            {"completeTask": {"task": "$.spawnedBy.id"}},
        ),
        terminal=frozenset({"done"}),
    )


def test_reactions_may_close_the_task_on_either_ending() -> None:
    schema = parse_approval_schema(
        _approved(
            {
                "invokeSkill": {
                    "skill": "x",
                    "onSuccess": [{"completeTask": {}}],
                    "onFailure": [{"comment": {"body": "no"}}, {"transition": {"status": "done"}}],
                }
            }
        ),
        statuses=frozenset({"todo", "done"}),
        terminal=frozenset({"done"}),
    )
    assert [a.when for a in schema.actions_for("default", "approved")] == [
        None,
        "onSuccess",
        "onFailure",
        "onFailure",
    ]


def test_skill_calls_list_every_invoke_skill_with_its_outcome() -> None:
    schema = parse_approval_schema(
        {
            "gates": {
                "default": {
                    "outcomes": {
                        "approved": [
                            {"comment": {"body": "x"}},
                            {"invokeSkill": {"skill": "git.merge@1"}},
                        ],
                        "rejected": [{"invokeSkill": {"skill": "notify"}}],
                    }
                }
            }
        }
    )
    assert [(c.outcome, c.path, c.name, c.version) for c in skill_calls(schema)] == [
        ("approved", "gates.default.outcomes.approved[1].invokeSkill.skill", "git.merge", "1"),
        ("rejected", "gates.default.outcomes.rejected[0].invokeSkill.skill", "notify", None),
    ]


@pytest.mark.parametrize(
    ("status", "code", "message", "revoked"),
    [
        ("cancelled", "basis_revoked", "task_terminal", "task_terminal"),
        ("cancelled", "basis_revoked", "approval_withdrawn", "approval_withdrawn"),
        # The basis stands, the call just did not happen: onFailure is told.
        ("cancelled", "basis_revoked", "skill_disabled", None),
        ("cancelled", "cancelled", "approval_outcome_skill_wait_expired", None),
        ("failed", "basis_revoked", "task_terminal", None),
    ],
)
def test_only_a_lost_basis_skips_both_reactions(
    status: str, code: str, message: str, revoked: str | None
) -> None:
    invocation = SkillInvocation(status=status, error={"code": code, "message": message})
    assert _basis_revoked(invocation) == revoked


# --- preconditions (TAI-ADR-0041 p.7) ------------------------------------------------

CI_GREEN: dict[str, Any] = {
    "observation": {"kind": "ci.status", "source": "ci", "task": "$.spawnedBy.id!"},
    "condition": {"eq": [{"var": "observation.data.conclusion"}, "success"]},
    "reason": "CI of $.spawnedBy.publicId is not green",
}


def _preconditions(*items: Any, outcome: str = "approved") -> dict[str, Any]:
    return {"gates": {"default": {"preconditions": {outcome: list(items)}}}}


def test_preconditions_of_approve_are_part_of_the_schema() -> None:
    schema = parse_approval_schema(
        {
            "gates": {
                "default": {
                    "preconditions": {"approved": [CI_GREEN]},
                    "outcomes": {"approved": [{"completeTask": {}}]},
                }
            }
        }
    )
    (precondition,) = schema.preconditions_for("default", "approved")
    assert precondition.kind == "ci.status"
    assert precondition.source == "ci"
    assert precondition.task == "$.spawnedBy.id!"
    assert precondition.condition == CI_GREEN["condition"]
    assert [p.text for p in precondition.paths()] == ["$.spawnedBy.id!", "$.spawnedBy.publicId"]
    assert schema.preconditions_for("default", "rejected") == ()
    assert [a.name for a in schema.actions_for("default", "approved")] == ["completeTask"]


def test_a_gate_may_declare_only_preconditions() -> None:
    schema = parse_approval_schema(_preconditions(CI_GREEN))
    assert schema.empty
    assert len(schema.preconditions_for("default", "approved")) == 1


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        # Rejecting must stay possible whatever the world looks like.
        (_preconditions(CI_GREEN, outcome="rejected"), "preconditions only of"),
        (_preconditions({**CI_GREEN, "when": True}), "unknown keys"),
        (_preconditions({"observation": {"kind": "ci.status"}}), "missing keys"),
        (_preconditions({**CI_GREEN, "observation": {"kind": "CI Status"}}), "observation kind"),
        (
            _preconditions({**CI_GREEN, "observation": {"kind": "ci.status", "source": "GitHub"}}),
            "observation source",
        ),
        (
            _preconditions({**CI_GREEN, "observation": {"kind": "ci", "task": "$.task.title"}}),
            "$.task.id or $.spawnedBy.id",
        ),
        (
            _preconditions({**CI_GREEN, "observation": {"kind": "ci", "task": "id $.task.id"}}),
            "$.task.id or $.spawnedBy.id",
        ),
        (
            _preconditions(
                {**CI_GREEN, "observation": {"kind": "ci", "externalRef": "$.approval.comment"}}
            ),
            "reads only $.task and $.spawnedBy",
        ),
        (_preconditions({**CI_GREEN, "reason": "Decided by $.approval.decidedBy"}), "reads only"),
        (_preconditions({**CI_GREEN, "condition": {"exists": "payload.x"}}), "unknown root"),
        (_preconditions({**CI_GREEN, "condition": {"regex": ["a", "b"]}}), "unknown operator"),
        ({"gates": {"default": {}}}, "'outcomes' and/or 'preconditions'"),
    ],
)
def test_invalid_preconditions_are_refused(document: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_approval_schema(document)
    assert caught.value.code == "invalid_approval_schema"
    assert fragment in caught.value.message
