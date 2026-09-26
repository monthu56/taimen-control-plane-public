"""ContextQueryRequest.strategy (TAI-ADR-0031 §6): briefing is a first-class mode."""

import pytest
from pydantic import ValidationError

from control_plane.api.v1.schemas import ContextQueryRequest


def test_strategy_accepts_known_modes() -> None:
    assert ContextQueryRequest(strategy="briefing").strategy == "briefing"
    assert ContextQueryRequest().strategy is None


def test_strategy_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        ContextQueryRequest(strategy="weird")
