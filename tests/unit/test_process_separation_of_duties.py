"""``excludedPrincipals`` of an ``approve`` step as the core takes them (CP-ADR-0074 §7)."""

import uuid

import pytest

from control_plane.application.commands.process_instances import _excluded_principals
from control_plane.domain.errors import ValidationError


def test_nobody_excluded_is_an_empty_list() -> None:
    assert _excluded_principals({}) == []
    assert _excluded_principals({"excludedPrincipals": []}) == []


def test_principal_ids_are_canonical_and_repeats_collapse() -> None:
    principal = uuid.uuid4()
    other = uuid.uuid4()
    listed = [str(principal).upper(), str(other), principal.hex, str(principal)]
    assert _excluded_principals({"excludedPrincipals": listed}) == [str(principal), str(other)]


@pytest.mark.parametrize(
    "listed",
    [
        "3f0c2b1e-7d7c-4a55-9f0e-5c8d2a1b4e6f",  # a string, not a list
        {"principal": "x"},
        ["uploader@example.test"],
        [str(uuid.uuid4()), 42],
        None,  # separationOfDuties computed to null
        [None],
        [""],
        [str(uuid.uuid4()), None],
    ],
)
def test_a_value_that_is_no_principal_is_refused_not_dropped(listed: object) -> None:
    with pytest.raises(ValidationError) as refused:
        _excluded_principals({"excludedPrincipals": listed})
    assert refused.value.code == "invalid_approval"
