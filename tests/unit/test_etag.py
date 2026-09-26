import pytest

from control_plane.api.etag import (
    BadIfMatchError,
    PreconditionRequiredError,
    format_task_etag,
    parse_if_match,
)


def test_format_and_parse_roundtrip() -> None:
    assert format_task_etag(7) == '"task-7"'
    assert parse_if_match('"task-7"') == 7


@pytest.mark.parametrize("header", ['"task-3"', "task-3", 'W/"task-3"', "  task-3  "])
def test_parse_accepts_variants(header: str) -> None:
    assert parse_if_match(header) == 3


def test_missing_if_match_is_precondition_required() -> None:
    with pytest.raises(PreconditionRequiredError) as excinfo:
        parse_if_match(None)
    assert excinfo.value.http_status == 428


@pytest.mark.parametrize("header", ["*", "task-", '"task-x"', "session-3", '"3"'])
def test_malformed_if_match_rejected(header: str) -> None:
    with pytest.raises(BadIfMatchError) as excinfo:
        parse_if_match(header)
    assert excinfo.value.http_status == 400
