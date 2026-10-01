"""Working-day arithmetic of ``cal.*`` (CP-ADR-0074 §9, CP-ADR-0075; process-packages P004).

The data is the production calendar of the Russian Federation for 2024 with
its moved days: the Saturdays 27 April, 2 November and 28 December are
working, the weekend of 6-7 January moved to 10 May and 31 December. A day
past the published years knows only the weekend and reads "provisional".
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from control_plane.api.v1.calendars import calendar_request, spec_as_sent
from control_plane.application.commands.process_instances import calendars_named
from control_plane.domain.calendar import Calendar, CalendarError, normalized_spec
from control_plane.domain.errors import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "processes" / "ru-2024.calendar.yaml").read_text("utf-8")
)
RU = Calendar.from_spec(DOCUMENT["spec"])


def _spec(**changes: Any) -> dict[str, Any]:
    return {**DOCUMENT["spec"], **changes}


def test_the_fixture_is_a_catalog_object_of_kind_calendar() -> None:
    schema = json.loads((FIXTURES / "superproject" / "object.schema.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    assert [error.message for error in validator.iter_errors(DOCUMENT)] == []


def test_2024_has_248_working_days() -> None:
    answer = RU.workdays_between(date(2023, 12, 31), date(2024, 12, 31))
    assert (answer.value, answer.provisional) == (248, False)


@pytest.mark.parametrize(
    ("day", "working"),
    [
        (date(2024, 1, 8), False),  # a Monday of the New Year holidays
        (date(2024, 1, 9), True),
        (date(2024, 2, 23), False),
        (date(2024, 4, 27), True),  # a Saturday made working
        (date(2024, 4, 29), False),  # the Monday it moved to
        (date(2024, 5, 10), False),  # the weekend of 6 January moved here
        (date(2024, 11, 2), True),
        (date(2024, 12, 28), True),
        (date(2024, 12, 29), False),  # a plain Sunday
        (date(2024, 12, 31), False),  # the weekend of 7 January moved here
        (date(2024, 7, 15), True),
    ],
)
def test_holidays_and_moved_working_days(day: date, working: bool) -> None:
    assert RU.is_workday(day).value is working
    assert RU.is_workday(day).provisional is False


def test_adding_working_days_steps_over_holidays_and_onto_working_saturdays() -> None:
    friday = date(2024, 4, 26)
    assert RU.add_workdays(friday, 1).value == date(2024, 4, 27)
    # 28 April is a Sunday, 29 April - 1 May are holidays.
    assert RU.add_workdays(friday, 2).value == date(2024, 5, 2)
    # 8 May, then 9-12 May off.
    assert RU.add_workdays(date(2024, 5, 7), 2).value == date(2024, 5, 13)
    assert RU.add_workdays(date(2024, 12, 27), 1).value == date(2024, 12, 28)
    assert RU.add_workdays(friday, 0).value == friday


def test_subtracting_working_days_walks_back_the_same_way() -> None:
    assert RU.add_workdays(date(2024, 5, 2), -1).value == date(2024, 4, 27)
    assert RU.add_workdays(date(2024, 5, 13), -2).value == date(2024, 5, 7)
    assert RU.add_workdays(date(2024, 1, 9), -1).value == date(2023, 12, 29)
    assert RU.add_workdays(date(2024, 5, 13), -2).provisional is False


def test_counting_between_dates_is_the_inverse_of_adding() -> None:
    assert RU.workdays_between(date(2024, 4, 26), date(2024, 5, 2)).value == 2
    assert RU.workdays_between(date(2024, 5, 2), date(2024, 4, 26)).value == -2
    assert RU.workdays_between(date(2024, 5, 2), date(2024, 5, 2)).value == 0
    start = date(2024, 1, 1)
    for n in range(0, 260, 7):
        end = RU.add_workdays(start, n).value
        assert RU.workdays_between(start, end).value == n


def test_leaving_the_published_years_makes_the_answer_provisional() -> None:
    # Only the weekend is known in 2023: Friday 29 December counts as working.
    assert RU.is_workday(date(2023, 12, 29)).provisional is True
    before = RU.add_workdays(date(2024, 1, 9), -1)
    assert (before.value, before.provisional) == (date(2023, 12, 29), True)
    later = RU.workdays_between(date(2024, 12, 20), date(2026, 1, 12))
    assert later.provisional is True
    unknown = Calendar.from_spec(_spec(years=[{"year": 2024}]))
    past = unknown.add_workdays(date(2024, 12, 28), 1)
    # 29 Dec is a Sunday, 30 and 31 Dec are plain weekdays without the holidays.
    assert (past.value, past.provisional) == (date(2024, 12, 30), False)
    beyond = unknown.add_workdays(date(2024, 12, 31), 1)
    assert (beyond.value, beyond.provisional) == (date(2025, 1, 1), True)


def test_a_provisional_year_marks_the_answer() -> None:
    assert RU.provisional_years == [2025]
    # 2025 publishes its January holidays but is not confirmed yet.
    answer = RU.add_workdays(date(2024, 12, 28), 1)
    assert (answer.value, answer.provisional) == (date(2025, 1, 9), True)
    assert RU.is_workday(date(2025, 1, 3)).value is False
    assert RU.is_workday(date(2025, 1, 3)).provisional is True
    assert RU.workdays_between(date(2024, 12, 1), date(2024, 12, 28)).provisional is False
    assert RU.workdays_between(date(2024, 12, 1), date(2025, 1, 1)).provisional is True


def test_short_days_are_known_but_count_as_working() -> None:
    assert RU.is_short_day(date(2024, 5, 8)).value is True
    assert RU.is_workday(date(2024, 5, 8)).value is True
    assert RU.is_short_day(date(2024, 5, 7)).value is False


def test_timestamps_are_read_in_the_calendar_timezone() -> None:
    # 21:30 UTC on Friday 26 April is 00:30 on the working Saturday in Moscow.
    ts = datetime(2024, 4, 26, 21, 30, tzinfo=UTC)
    assert RU.local_date(ts) == date(2024, 4, 27)
    assert RU.is_workday_at(ts).value is True
    moved = RU.add_workdays_at(ts, 1)
    assert moved.value == datetime(2024, 5, 1, 21, 30, tzinfo=UTC)
    assert moved.provisional is False
    assert RU.workdays_between_at(ts, moved.value).value == 1
    with pytest.raises(CalendarError) as caught:
        RU.is_workday_at(datetime(2024, 4, 26, 12))
    assert caught.value.code == "naive_timestamp"


def test_the_weekend_is_configurable() -> None:
    friday_off = Calendar.from_spec(_spec(weekend=[5, 6], years=[{"year": 2024}]))
    assert friday_off.is_workday(date(2024, 7, 12)).value is False  # Friday
    assert friday_off.is_workday(date(2024, 7, 14)).value is True  # Sunday
    assert Calendar.from_spec(_spec(years=[{"year": 2024}])).weekend == {6, 7}
    everyday = Calendar.from_spec(_spec(weekend=[], years=[{"year": 2024}]))
    assert everyday.is_workday(date(2024, 7, 14)).value is True


@pytest.mark.parametrize(
    ("spec", "code", "path"),
    [
        (_spec(timezone="Mars/Olympus"), "unknown_timezone", "/spec/timezone"),
        (
            _spec(years=[{"year": 2024}, {"year": 2024}]),
            "duplicate_calendar_year",
            "/spec/years/1",
        ),
        (
            _spec(years=[{"year": 2024, "holidays": ["2024-01-01", "2025-01-01"]}]),
            "calendar_date_outside_year",
            "/spec/years/0/holidays/1",
        ),
        (
            _spec(years=[{"year": 2024, "holidays": ["2024-01-06"], "workdays": ["2024-01-06"]}]),
            "calendar_day_conflict",
            "/spec/years/0",
        ),
    ],
)
def test_what_the_schema_cannot_check_is_refused(
    spec: dict[str, Any], code: str, path: str
) -> None:
    with pytest.raises(CalendarError) as caught:
        Calendar.from_spec(spec)
    assert (caught.value.code, caught.value.path) == (code, path)


def test_a_calendar_without_working_days_ends_in_an_error() -> None:
    never = Calendar.from_spec(_spec(weekend=[1, 2, 3, 4, 5, 6, 7], years=[{"year": 2024}]))
    with pytest.raises(CalendarError) as caught:
        never.add_workdays(date(2024, 1, 1), 1)
    assert caught.value.code == "calendar_scan_limit"
    with pytest.raises(CalendarError):
        RU.workdays_between(date(2000, 1, 1), date(2000, 1, 1) + timedelta(days=366 * 51))


def test_the_order_of_years_and_dates_is_not_content() -> None:
    shuffled = _spec(
        weekend=[7, 6],
        years=[
            {**year, **{k: list(reversed(v)) for k, v in year.items() if isinstance(v, list)}}
            for year in reversed(DOCUMENT["spec"]["years"])
        ],
    )
    assert normalized_spec(shuffled) == normalized_spec(_spec())
    assert normalized_spec(_spec())["years"][0]["holidays"][0] == "2024-01-01"


def test_a_package_object_of_kind_calendar_becomes_a_publish_request() -> None:
    request = calendar_request(DOCUMENT)
    assert request.key == "ru"
    assert spec_as_sent(request) == DOCUMENT["spec"]
    with pytest.raises(ValidationError) as caught:
        calendar_request({**DOCUMENT, "kind": "Process"})
    assert caught.value.code == "invalid_calendar"
    with pytest.raises(ValidationError) as caught:
        calendar_request({**DOCUMENT, "spec": {**DOCUMENT["spec"], "years": []}})
    assert caught.value.details["errors"][0]["path"] == "/spec/years"


async def test_the_package_calendars_a_plan_names_stand_for_the_published_ones() -> None:
    """``calendars_named`` with ``own``: no query for them, no version number (TASK-001161)."""

    class NoDatabase:
        async def scalars(self, *_: Any) -> Any:
            raise AssertionError("every calendar named is the package's own")

    session: Any = NoDatabase()
    other = Calendar.from_spec(_spec())
    found = await calendars_named(
        session, uuid.uuid4(), {"calendar": "ru"}, own={"ru": RU, "x": other}
    )
    assert found == ({"ru": RU}, {})
    assert await calendars_named(session, uuid.uuid4(), {}, own={"ru": RU}) == ({}, {})
    assert await calendars_named(session, uuid.uuid4(), {}) == ({}, {})
