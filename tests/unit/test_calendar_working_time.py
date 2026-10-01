"""Working time of a calendar: the control set of SC-004 (CP-ADR-0078 §2, P008).

The data is the calendar ``ru`` of platform-calendars for 2025-2027 with
09:00-18:00 working hours and short days one hour shorter (09:00-17:00). Every
expected value is counted by hand from that calendar; the comment of a case
shows the count. Times are Moscow wall-clock times unless a case says UTC.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from control_plane.application.commands.calendars import check_calendar_spec
from control_plane.domain.calendar import Calendar, CalendarError, WorkingHours, normalized_spec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENT: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "processes" / "ru-2025-2027.calendar.yaml").read_text("utf-8")
)
RU = Calendar.from_spec(DOCUMENT["spec"])
H = timedelta(hours=1)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _spec(**changes: Any) -> dict[str, Any]:
    return {**DOCUMENT["spec"], **changes}


def test_the_fixture_is_a_catalog_object_of_kind_calendar() -> None:
    schema = json.loads((FIXTURES / "superproject" / "object.schema.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    assert [error.message for error in validator.iter_errors(DOCUMENT)] == []


# --- the control set --------------------------------------------------------------------

ADD_CASES = [
    pytest.param(
        # Fri 16-18 is 2h; Sat and Sun are off; Mon 09-11 is the other 2h.
        "2026-10-02T16:00",
        4 * H,
        "2026-10-05T11:00",
        id="weekend",
    ),
    pytest.param(
        # Fri 17-18 is 1h; 7-8 Mar is the weekend, Mon 9 Mar the day off for 8 March.
        "2026-03-06T17:00",
        2 * H,
        "2026-03-10T10:00",
        id="8-march-2026",
    ),
    pytest.param(
        # Fri 7 Mar is short (to 17:00): 1h; 8 Mar is a Saturday, Mon 10 Mar works.
        "2025-03-07T16:00",
        2 * H,
        "2025-03-10T10:00",
        id="8-march-2025-after-a-short-day",
    ),
    pytest.param(
        # Fri 31 Oct 17-18 is 1h; Sat 1 Nov is a moved working day (short, 09-17).
        "2025-10-31T17:00",
        3 * H,
        "2025-11-01T11:00",
        id="moved-working-saturday",
    ),
    pytest.param(
        # Fri 19 Feb 1h, Sat 20 Feb (moved, short) 8h; 22-23 Feb off; Wed 24 Feb 1h.
        "2027-02-19T17:00",
        10 * H,
        "2027-02-24T10:00",
        id="moved-short-saturday-then-holidays",
    ),
    pytest.param(
        # Thu 30 Apr is short: 15-17 is exactly 2h and ends at 17:00.
        "2026-04-30T15:00",
        2 * H,
        "2026-04-30T17:00",
        id="short-day-end",
    ),
    pytest.param(
        # Thu 30 Apr 15-17 is 2h; 1 May holiday, 2-3 May weekend; Mon 4 May 1h.
        "2026-04-30T15:00",
        3 * H,
        "2026-05-04T10:00",
        id="short-day-into-may",
    ),
    pytest.param(
        # A whole day from its start ends at 18:00, not at 09:00 of the next day.
        "2026-10-06T09:00",
        9 * H,
        "2026-10-06T18:00",
        id="interval-end",
    ),
    pytest.param(
        # From the end of the day the count starts the next morning.
        "2026-10-06T18:00",
        1 * H,
        "2026-10-07T10:00",
        id="from-interval-end",
    ),
    pytest.param(
        # Before hours the count starts at 09:00.
        "2026-10-06T07:30",
        30 * timedelta(minutes=1),
        "2026-10-06T09:30",
        id="before-hours",
    ),
    pytest.param(
        # Wed 30 Dec 16-18 is 2h; 31 Dec and 1-8 Jan 2027 are off, 9-10 Jan the weekend.
        "2026-12-30T16:00",
        4 * H,
        "2027-01-11T11:00",
        id="year-change-2026-2027",
    ),
    pytest.param(
        # Tue 30 Dec 17-18 is 1h; 31 Dec and 1-9 Jan 2026 are off, 10-11 Jan the weekend.
        "2025-12-30T17:00",
        2 * H,
        "2026-01-12T10:00",
        id="year-change-2025-2026",
    ),
    pytest.param(
        # Mon 5 Oct 09-10 is 1h back; Fri 2 Oct 17-18 the other.
        "2026-10-05T10:00",
        -2 * H,
        "2026-10-02T17:00",
        id="backwards-over-weekend",
    ),
]


@pytest.mark.parametrize(("start", "amount", "expected"), ADD_CASES)
def test_adding_working_time(start: str, amount: timedelta, expected: str) -> None:
    answer = RU.add_working_time(_at(start), amount)
    assert answer.value == _at(expected)
    assert answer.provisional is False


BETWEEN_CASES = [
    pytest.param(
        # Wed 10 Jun 9h, Thu 11 Jun short 8h, 12 Jun holiday, 13-14 weekend, Mon 15 Jun 9h.
        "2026-06-10T09:00",
        "2026-06-15T18:00",
        26 * H,
        id="holiday-and-short-day",
    ),
    pytest.param(
        # Fri 31 Oct 9h, Sat 1 Nov (moved, short) 8h, 2 Nov Sunday, 3-4 Nov off.
        "2025-10-31T09:00",
        "2025-11-05T09:00",
        17 * H,
        id="moved-working-saturday",
    ),
    pytest.param(
        # Outside hours on both ends: Fri 20:00 to Mon 08:00 has no working time.
        "2026-10-02T20:00",
        "2026-10-05T08:00",
        timedelta(0),
        id="weekend-night",
    ),
    pytest.param(
        "2026-06-15T18:00",
        "2026-06-10T09:00",
        -26 * H,
        id="backwards",
    ),
]


@pytest.mark.parametrize(("a", "b", "expected"), BETWEEN_CASES)
def test_working_time_between(a: str, b: str, expected: timedelta) -> None:
    answer = RU.working_time_between(_at(a), _at(b))
    assert (answer.value, answer.provisional) == (expected, False)


def test_a_pause_over_the_weekend_keeps_the_remainder() -> None:
    """A 16h deadline paused on Thursday and resumed on Saturday.

    Entered Wed 7 Oct 10:00, paused Thu 8 Oct 12:00: Wed 10-18 is 8h and Thu
    09-12 is 3h, so 5h remain. Resumed Sat 10 Oct 14:00: Mon 12 Oct 09-14.
    """
    entered, paused = _at("2026-10-07T10:00"), _at("2026-10-08T12:00")
    assert RU.add_working_time(entered, 16 * H).value == _at("2026-10-08T17:00")
    spent = RU.working_time_between(entered, paused).value
    assert spent == 11 * H
    answer = RU.add_working_time(_at("2026-10-10T14:00"), 16 * H - spent)
    assert (answer.value, answer.provisional) == (_at("2026-10-12T14:00"), False)


def test_a_year_the_calendar_does_not_publish_is_provisional() -> None:
    """Thu 30 Dec 2027 17-18 is 1h; 31 Dec is off; 2028 knows only the weekend.

    1-2 Jan 2028 are Saturday and Sunday, Mon 3 Jan reads as working, provisionally.
    """
    answer = RU.add_working_time(_at("2027-12-30T17:00"), 2 * H)
    assert (answer.value, answer.provisional) == (_at("2028-01-03T10:00"), True)
    inside = RU.add_working_time(_at("2027-12-30T17:00"), 1 * H)
    assert (inside.value, inside.provisional) == (_at("2027-12-30T18:00"), False)
    between = RU.working_time_between(_at("2027-12-30T09:00"), _at("2028-01-03T18:00"))
    assert (between.value, between.provisional) == (18 * H, True)


def test_a_year_marked_provisional_is_provisional() -> None:
    spec = _spec(years=[{"year": 2026, "provisional": True}])
    answer = Calendar.from_spec(spec).add_working_time(_at("2026-10-02T16:00"), 4 * H)
    assert (answer.value, answer.provisional) == (_at("2026-10-05T11:00"), True)


def test_the_instant_forms_answer_in_the_offset_they_are_given() -> None:
    # 13:00 UTC is 16:00 in Moscow: the weekend case, 11:00 Moscow is 08:00 UTC.
    answer = RU.add_working_time_at(datetime(2026, 10, 2, 13, tzinfo=UTC), 4 * H)
    assert answer.value == datetime(2026, 10, 5, 8, tzinfo=UTC)
    assert answer.value.tzinfo is UTC
    between = RU.working_time_between_at(
        datetime(2026, 10, 2, 13, tzinfo=UTC), datetime(2026, 10, 5, 8, tzinfo=UTC)
    )
    assert (between.value, between.provisional) == (4 * H, False)


def test_adding_the_time_between_lands_on_the_later_moment() -> None:
    a = _at("2025-12-29T11:15")
    for b in ("2025-12-30T17:59", "2026-01-12T09:01", "2026-03-10T12:00", "2027-01-11T11:00"):
        between = RU.working_time_between(a, _at(b)).value
        assert RU.add_working_time(a, between).value == _at(b)
        assert RU.add_working_time(_at(b), -between).value == a


# --- working hours: the shape ----------------------------------------------------------


def test_a_weekday_takes_its_own_intervals_and_a_moved_day_the_plain_ones() -> None:
    hours = {
        "intervals": [{"from": "09:00", "to": "18:00"}],
        "weekdays": {5: [{"from": "09:00", "to": "16:45"}], "6": []},
        "shortDayReduction": "PT1H",
    }
    calendar = Calendar.from_spec(_spec(workingHours=hours))
    # Friday ends at 16:45: 16:00-16:45, then Monday 09:00-09:15.
    friday = calendar.add_working_time(_at("2026-10-02T16:00"), H)
    assert friday.value == _at("2026-10-05T09:15")
    # Sat 1 Nov 2025 is a moved working day: the plain intervals, less the short hour.
    assert calendar.working_intervals(date(2025, 11, 1)) == (((540, 1020),), False)
    assert calendar.working_intervals(date(2025, 11, 2)) == ((), False)


def test_the_reduction_comes_off_the_end_of_the_last_interval_and_further() -> None:
    hours = WorkingHours(intervals=((540, 780), (840, 1080)), weekdays={})
    assert hours.of_day(1, moved=False, short=True) == ((540, 780), (840, 1080))
    one = WorkingHours(intervals=hours.intervals, weekdays={}, short_day_reduction=60)
    assert one.of_day(1, moved=False, short=True) == ((540, 780), (840, 1020))
    assert one.of_day(1, moved=False, short=False) == ((540, 780), (840, 1080))
    five = WorkingHours(intervals=hours.intervals, weekdays={}, short_day_reduction=300)
    assert five.of_day(1, moved=False, short=True) == ((540, 720),)


def test_an_interval_may_end_at_midnight() -> None:
    hours = {"intervals": [{"from": "20:00", "to": "24:00"}]}
    calendar = Calendar.from_spec(_spec(workingHours=hours))
    answer = calendar.add_working_time(_at("2026-10-06T21:00"), 4 * H)
    assert answer.value == _at("2026-10-07T21:00")
    assert calendar.add_working_time(_at("2026-10-06T21:00"), 3 * H).value == _at(
        "2026-10-07T00:00"
    )


@pytest.mark.parametrize(
    ("hours", "path"),
    [
        (
            {"intervals": [{"from": "09:00", "to": "13:00"}, {"from": "12:00", "to": "18:00"}]},
            "/spec/workingHours/intervals/1",
        ),
        ({"intervals": [{"from": "18:00", "to": "09:00"}]}, "/spec/workingHours/intervals/0"),
        ({"intervals": [{"from": "09:00", "to": "09:00"}]}, "/spec/workingHours/intervals/0"),
        ({"intervals": [{"from": "09:00", "to": "24:30"}]}, "/spec/workingHours/intervals/0/to"),
        ({"intervals": []}, "/spec/workingHours/intervals"),
        (
            {"intervals": [{"from": "09:00", "to": "18:00"}], "weekdays": {"8": []}},
            "/spec/workingHours/weekdays/8",
        ),
        (
            {"intervals": [{"from": "09:00", "to": "18:00"}], "shortDayReduction": "P1M"},
            "/spec/workingHours/shortDayReduction",
        ),
        (
            {"intervals": [{"from": "09:00", "to": "18:00"}], "shortDayReduction": "PT30S"},
            "/spec/workingHours/shortDayReduction",
        ),
    ],
)
def test_working_hours_the_schema_cannot_check_are_refused(
    hours: dict[str, Any], path: str
) -> None:
    with pytest.raises(CalendarError) as caught:
        Calendar.from_spec(_spec(workingHours=hours))
    assert (caught.value.code, caught.value.path) == ("invalid_working_hours", path)


def test_a_calendar_without_working_hours_has_no_working_time() -> None:
    spec = {key: value for key, value in DOCUMENT["spec"].items() if key != "workingHours"}
    calendar = Calendar.from_spec(spec)
    for call in (
        lambda: calendar.add_working_time(_at("2026-10-02T16:00"), H),
        lambda: calendar.working_time_between(_at("2026-10-02T16:00"), _at("2026-10-05T16:00")),
        lambda: calendar.working_intervals(date(2026, 10, 2)),
    ):
        with pytest.raises(CalendarError) as caught:
            call()
        assert caught.value.code == "calendar_without_hours"


def test_local_and_instant_forms_do_not_mix() -> None:
    with pytest.raises(CalendarError) as local:
        RU.add_working_time(datetime(2026, 10, 2, 13, tzinfo=UTC), H)
    assert local.value.code == "aware_local_time"
    with pytest.raises(CalendarError) as instant:
        RU.add_working_time_at(_at("2026-10-02T13:00"), H)
    assert instant.value.code == "naive_timestamp"


def test_a_calendar_of_no_working_hours_ends_in_an_error() -> None:
    days = {str(day): [] for day in range(1, 8)}
    hours = {"intervals": [{"from": "09:00", "to": "18:00"}], "weekdays": days}
    calendar = Calendar.from_spec(_spec(workingHours=hours, years=[{"year": 2026}]))
    with pytest.raises(CalendarError) as caught:
        calendar.add_working_time(_at("2026-10-02T16:00"), H)
    assert caught.value.code == "calendar_scan_limit"


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        pytest.param("2026-10-02T16:00", "2026-10-02T16:00", id="inside"),
        pytest.param("2026-10-02T07:30", "2026-10-02T09:00", id="before-the-day"),
        pytest.param("2026-10-02T18:00", "2026-10-05T09:00", id="the-end-is-outside"),
        pytest.param("2026-10-03T12:00", "2026-10-05T09:00", id="weekend"),
        pytest.param("2026-03-06T19:00", "2026-03-10T09:00", id="8-march-2026"),
    ],
)
def test_the_next_working_time(start: str, expected: str) -> None:
    """Where a ``workdays`` deadline starts counting from (CP-ADR-0078 §1)."""
    assert RU.next_working_time(_at(start)).value == _at(expected)


def test_the_next_working_time_of_an_instant() -> None:
    # Saturday 12:00 in Moscow is 09:00 UTC; Monday 09:00 Moscow is 06:00 UTC.
    answer = RU.next_working_time_at(datetime(2026, 10, 3, 9, tzinfo=UTC))
    assert answer.value == datetime(2026, 10, 5, 6, tzinfo=UTC)
    assert answer.value.tzinfo is UTC
    assert RU.next_working_time_at(datetime(2028, 1, 3, 9, tzinfo=UTC)).provisional is True


# --- the canonical form --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "calendar_hash"),
    [
        # The hashes of these fixtures before CP-ADR-0078: a published version stays the same.
        (
            "ru-2024.calendar.yaml",
            "sha256:37fe165a9cb55474758d7f07329c9cd43d6e76c21357067273a67159d0b658f7",
        ),
        (
            "ru.calendar.yaml",
            "sha256:ea51f236f08e872423d826af4fc4f789707d9f285450f6a5143fefaaeb18e208",
        ),
    ],
)
def test_a_calendar_without_working_hours_hashes_as_before(name: str, calendar_hash: str) -> None:
    spec = yaml.safe_load((FIXTURES / "processes" / name).read_text("utf-8"))["spec"]
    body, found, _ = check_calendar_spec(spec)
    assert found == calendar_hash
    assert "workingHours" not in body
    assert check_calendar_spec({**spec, "workingHours": None})[1] == calendar_hash


def test_working_hours_make_a_new_version_and_weekday_keys_read_as_strings() -> None:
    spec = {key: value for key, value in DOCUMENT["spec"].items() if key != "workingHours"}
    hours = {"intervals": [{"from": "09:00", "to": "18:00"}], "weekdays": {6: []}}
    with_hours = check_calendar_spec({**spec, "workingHours": hours})
    assert with_hours[1] != check_calendar_spec(spec)[1]
    assert with_hours[0]["workingHours"]["weekdays"] == {"6": []}
    as_json = {**hours, "weekdays": {"6": []}}
    assert check_calendar_spec({**spec, "workingHours": as_json})[1] == with_hours[1]
    assert normalized_spec({**spec, "workingHours": hours})["workingHours"]["weekdays"] == {"6": []}
