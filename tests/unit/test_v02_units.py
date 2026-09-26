import uuid

import pytest

from control_plane.api.etag import format_etag, parse_if_match
from control_plane.application.commands.eligibility import RequirementSpec
from control_plane.application.commands.relations import _needs_edge
from control_plane.domain.enums import (
    BLOCKING_RELATION_TYPES,
    Permission,
    TaskRelationType,
)
from control_plane.domain.errors import DomainError


def test_needs_edge_normalization() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    # A depends_on B: A is the dependent, B the prerequisite.
    assert _needs_edge(TaskRelationType.DEPENDS_ON, a, b) == (a, b)
    # A blocks B: B is the dependent, A the prerequisite.
    assert _needs_edge(TaskRelationType.BLOCKS, a, b) == (b, a)


def test_blocking_relation_types() -> None:
    assert TaskRelationType.DEPENDS_ON in BLOCKING_RELATION_TYPES
    assert TaskRelationType.BLOCKS in BLOCKING_RELATION_TYPES
    assert TaskRelationType.RELATED_TO not in BLOCKING_RELATION_TYPES
    assert TaskRelationType.PARENT not in BLOCKING_RELATION_TYPES


def test_requirement_spec_empty() -> None:
    assert RequirementSpec().is_empty()
    assert not RequirementSpec(roles=["x"]).is_empty()
    assert not RequirementSpec(skills=["x"]).is_empty()


def test_etag_entities() -> None:
    assert format_etag("workspace", 3) == '"workspace-3"'
    assert parse_if_match('"workspace-3"', "workspace") == 3
    assert parse_if_match("skill-7", "skill") == 7
    # Wrong entity prefix is rejected.
    with pytest.raises(DomainError) as excinfo:
        parse_if_match('"task-3"', "workspace")
    assert excinfo.value.code == "invalid_if_match"
    with pytest.raises(DomainError) as excinfo:
        parse_if_match(None, "role")
    assert excinfo.value.http_status == 428


def test_v02_permissions_registered() -> None:
    for permission in (
        "workspaces.read",
        "workspaces.manage",
        "org.read",
        "org.manage",
        "artifacts.read",
        "artifacts.write",
        "approvals.read",
        "approvals.manage",
        "approvals.decide",
    ):
        assert permission in {p.value for p in Permission}
