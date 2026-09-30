"""CONTROL_PLANE_AGENT_RETRY_WINDOW: a clear configuration error, not a bare ValueError."""

import pytest

from control_plane_agent.main import RETRY_WINDOW_SECONDS, RetryWindowError, retry_window_from_env


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_unset_is_the_default(raw: str | None) -> None:
    environ = {} if raw is None else {"CONTROL_PLANE_AGENT_RETRY_WINDOW": raw}
    assert retry_window_from_env(environ) == RETRY_WINDOW_SECONDS == 300.0


@pytest.mark.parametrize(("raw", "window"), [("0", 0.0), ("120", 120.0), (" 7.5 ", 7.5)])
def test_a_number_of_seconds_is_taken(raw: str, window: float) -> None:
    assert retry_window_from_env({"CONTROL_PLANE_AGENT_RETRY_WINDOW": raw}) == window


@pytest.mark.parametrize("raw", ["5m", "abc", "-1", "nan", "inf", "-inf", "1e999"])
def test_a_bad_value_names_the_variable(raw: str) -> None:
    with pytest.raises(RetryWindowError) as caught:
        retry_window_from_env({"CONTROL_PLANE_AGENT_RETRY_WINDOW": raw})
    message = str(caught.value)
    assert "CONTROL_PLANE_AGENT_RETRY_WINDOW" in message
    assert repr(raw) in message
