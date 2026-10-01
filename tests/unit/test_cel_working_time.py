"""``cal.addWorkingTime`` and ``cal.workingTimeBetween`` of the CEL profile (CP-ADR-0078 §2, P009).

The cases are the control set of P008 (``test_calendar_working_time.py``):
the calendar ``ru`` of 2025-2027 with 09:00-18:00 working hours, Moscow
wall-clock times. Here they go through CEL, with the calendar key and with
the process calendar, as ``cal.addWorkdays`` does.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from control_plane.domain.calendar import Calendar
from control_plane.domain.cel_profile import (
    EXPRESSION_ERROR,
    EXPRESSION_TYPE_ERROR,
    Environment,
    ExpressionError,
    environment,
)
from tests.unit.test_calendar_working_time import ADD_CASES, BETWEEN_CASES, RU

MOSCOW = ZoneInfo("Europe/Moscow")
H = timedelta(hours=1)
DATA = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "format": "date-time"},
        "b": {"type": "string", "format": "date-time"},
        "amount": {"type": "string", "format": "duration"},
    },
}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
# The calendar of the earlier fixtures: working days, no working hours.
RU_2024 = Calendar.from_spec(
    yaml.safe_load((FIXTURES / "ru-2024.calendar.yaml").read_text("utf-8"))["spec"]
)


def _env(calendar: str | None = None) -> Environment:
    return environment(data=DATA, calendar=calendar)


def _moscow(text: str) -> str:
    return datetime.fromisoformat(text).replace(tzinfo=MOSCOW).isoformat()


def _iso(amount: timedelta) -> str:
    seconds = int(amount.total_seconds())
    return f"{'-' if seconds < 0 else ''}PT{abs(seconds)}S"


def _values(**data: Any) -> dict[str, Any]:
    return {"data": data}


@pytest.mark.parametrize("keyed", [True, False], ids=["key", "process-calendar"])
@pytest.mark.parametrize(("start", "amount", "expected"), ADD_CASES)
def test_add_working_time(start: str, amount: timedelta, expected: str, keyed: bool) -> None:
    env = _env(None if keyed else "ru")
    key = ', "ru"' if keyed else ""
    program = env.compile(f"cal.addWorkingTime(data.a, data.amount{key})")
    result = program.evaluate(_values(a=_moscow(start), amount=_iso(amount)), calendars={"ru": RU})
    assert result.value == datetime.fromisoformat(expected).replace(tzinfo=MOSCOW)
    assert result.provisional is False


@pytest.mark.parametrize("keyed", [True, False], ids=["key", "process-calendar"])
@pytest.mark.parametrize(("a", "b", "expected"), BETWEEN_CASES)
def test_working_time_between(a: str, b: str, expected: timedelta, keyed: bool) -> None:
    env = _env(None if keyed else "ru")
    key = ', "ru"' if keyed else ""
    program = env.compile(f"cal.workingTimeBetween(data.a, data.b{key})")
    result = program.evaluate(_values(a=_moscow(a), b=_moscow(b)), calendars={"ru": RU})
    assert (result.value, result.provisional) == (expected, False)


def test_the_types_of_the_functions() -> None:
    env = _env("ru")
    assert env.compile('cal.addWorkingTime(data.a, duration("PT8H")) > data.b').output_type
    with pytest.raises(ExpressionError) as caught:
        env.compile("cal.addWorkingTime(data.a, 8)")
    assert caught.value.code == EXPRESSION_TYPE_ERROR
    compared = env.compile('cal.workingTimeBetween(data.a, data.b) > duration("PT1H")')
    result = compared.evaluate(
        _values(a="2026-10-06T06:00:00Z", b="2026-10-06T15:00:00Z"), calendars={"ru": RU}
    )
    assert result.value is True


def test_without_a_process_calendar_the_key_is_required() -> None:
    for expression in (
        "cal.addWorkingTime(data.a, data.amount)",
        "cal.workingTimeBetween(data.a, data.b)",
    ):
        with pytest.raises(ExpressionError) as caught:
            _env().compile(expression)
        assert caught.value.code == EXPRESSION_TYPE_ERROR


def test_a_year_the_calendar_does_not_publish_marks_the_result() -> None:
    # Thu 30 Dec 2027 17-18 is 1h; 31 Dec is off; Mon 3 Jan 2028 reads as working.
    program = _env().compile('cal.addWorkingTime(data.a, duration("PT2H"), "ru")')
    result = program.evaluate(_values(a=_moscow("2027-12-30T17:00")), calendars={"ru": RU})
    assert result.value == datetime(2028, 1, 3, 10, tzinfo=MOSCOW)
    assert result.provisional is True
    between = _env().compile('cal.workingTimeBetween(data.a, data.b, "ru")')
    result = between.evaluate(
        _values(a=_moscow("2027-12-30T09:00"), b=_moscow("2028-01-03T18:00")),
        calendars={"ru": RU},
    )
    assert (result.value, result.provisional) == (18 * H, True)


def test_the_answer_is_the_instant_whatever_the_offset_of_the_input() -> None:
    # 13:00 UTC is 16:00 in Moscow: the weekend case of the control set.
    program = _env().compile('cal.addWorkingTime(data.a, duration("PT4H"), "ru")')
    result = program.evaluate(_values(a="2026-10-02T13:00:00Z"), calendars={"ru": RU})
    assert result.value == datetime(2026, 10, 5, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression",
    [
        'cal.addWorkingTime(data.a, duration("PT1H"), "ru")',
        'cal.workingTimeBetween(data.a, data.b, "ru")',
    ],
)
def test_a_calendar_without_working_hours_is_an_error(expression: str) -> None:
    with pytest.raises(ExpressionError) as caught:
        _env().compile(expression).evaluate(
            _values(a="2024-05-06T09:00:00Z", b="2024-05-07T09:00:00Z"),
            calendars={"ru": RU_2024},
        )
    assert caught.value.code == EXPRESSION_ERROR
    assert caught.value.details["reason"] == "calendar_without_hours"


def test_a_calendar_the_evaluation_lacks_is_an_error() -> None:
    with pytest.raises(ExpressionError) as caught:
        _env().compile('cal.addWorkingTime(data.a, duration("PT1H"), "kz")').evaluate(
            _values(a="2026-10-06T06:00:00Z"), calendars={"ru": RU}
        )
    assert caught.value.code == EXPRESSION_ERROR
    assert caught.value.details["reason"] == "calendar_missing"
