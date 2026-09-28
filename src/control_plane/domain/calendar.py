"""Working-day calendars: the arithmetic behind ``cal.*`` (CP-ADR-0074 §9, CP-ADR-0075).

A calendar is a timezone, the ISO weekdays that are weekend, and per
published year its holidays, the working days moved onto a weekend and the
shortened days. A day of a published year is a working day when it is a moved
working day, otherwise when it is neither a holiday nor a weekend day. A day
outside every published year knows only the weekend.

Every answer carries ``provisional``: it is true when the computation looked
at a day of a year marked ``provisional`` or of a year the calendar does not
publish at all. A deadline then reads "provisional" until a confirmed year
replaces the guess (a new calendar version reschedules the timers).

Pure functions over plain values: the engine, the package tests and replay
compute the same answer from the same calendar version.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_WEEKEND: frozenset[int] = frozenset({6, 7})
# One step of cal.* never walks further than this: a calendar whose every day
# is non-working must end in an error, not in an endless scan.
MAX_SCAN_DAYS = 366 * 50
_ONE_DAY = timedelta(days=1)


class CalendarError(ValueError):
    """The calendar or the question cannot give an answer."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True)
class CalendarYear:
    year: int
    provisional: bool = False
    holidays: frozenset[date] = frozenset()
    workdays: frozenset[date] = frozenset()
    short_days: frozenset[date] = frozenset()


@dataclass(frozen=True)
class DayAnswer:
    value: bool
    provisional: bool


@dataclass(frozen=True)
class DateAnswer:
    value: date
    provisional: bool


@dataclass(frozen=True)
class CountAnswer:
    value: int
    provisional: bool


@dataclass(frozen=True)
class TimestampAnswer:
    value: datetime
    provisional: bool


def _dates(raw: Iterable[Any] | None, *, year: int, path: str) -> frozenset[date]:
    found: set[date] = set()
    for index, item in enumerate(raw or ()):
        day = item if isinstance(item, date) else date.fromisoformat(item)
        if day.year != year:
            raise CalendarError(
                "calendar_date_outside_year",
                f"{day.isoformat()} does not belong to the year {year}",
                path=f"{path}/{index}",
            )
        found.add(day)
    return frozenset(found)


@dataclass(frozen=True)
class Calendar:
    timezone: str
    weekend: frozenset[int]
    years: Mapping[int, CalendarYear]

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "Calendar":
        """A calendar from ``$defs.calendarSpec`` (camelCase, dates as ISO strings).

        The shape is the schema's business; this checks what the schema
        cannot: a known timezone, one entry per year, dates inside their year,
        no day both a holiday and a moved working day.
        """
        timezone = spec["timezone"]
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CalendarError(
                "unknown_timezone", f"Unknown timezone {timezone!r}", path="/spec/timezone"
            ) from exc
        raw_weekend = spec.get("weekend")
        weekend = DEFAULT_WEEKEND if raw_weekend is None else frozenset(raw_weekend)
        years: dict[int, CalendarYear] = {}
        for index, raw in enumerate(spec["years"]):
            path = f"/spec/years/{index}"
            number = raw["year"]
            if number in years:
                raise CalendarError(
                    "duplicate_calendar_year", f"The year {number} is listed twice", path=path
                )
            entry = CalendarYear(
                year=number,
                provisional=bool(raw.get("provisional", False)),
                holidays=_dates(raw.get("holidays"), year=number, path=f"{path}/holidays"),
                workdays=_dates(raw.get("workdays"), year=number, path=f"{path}/workdays"),
                short_days=_dates(raw.get("shortDays"), year=number, path=f"{path}/shortDays"),
            )
            both = entry.holidays & entry.workdays
            if both:
                raise CalendarError(
                    "calendar_day_conflict",
                    f"{min(both).isoformat()} is both a holiday and a working day",
                    path=path,
                )
            years[number] = entry
        return cls(timezone=timezone, weekend=weekend, years=years)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def provisional_years(self) -> list[int]:
        return sorted(number for number, entry in self.years.items() if entry.provisional)

    # --- one day ------------------------------------------------------------

    def _day(self, day: date) -> tuple[bool, bool]:
        entry = self.years.get(day.year)
        if entry is None:
            return day.isoweekday() not in self.weekend, True
        if day in entry.workdays:
            working = True
        elif day in entry.holidays:
            working = False
        else:
            working = day.isoweekday() not in self.weekend
        return working, entry.provisional

    def is_workday(self, day: date) -> DayAnswer:
        working, provisional = self._day(day)
        return DayAnswer(working, provisional)

    def is_short_day(self, day: date) -> DayAnswer:
        entry = self.years.get(day.year)
        if entry is None:
            return DayAnswer(False, True)
        return DayAnswer(day in entry.short_days, entry.provisional)

    # --- spans ---------------------------------------------------------------

    def add_workdays(self, start: date, n: int) -> DateAnswer:
        """The ``n``-th working day after ``start`` (before it when ``n`` < 0).

        ``start`` itself is never counted, so ``n`` = 1 from a Friday before a
        plain weekend is Monday; ``n`` = 0 is ``start`` as it is. The answer is
        provisional when any day it stepped over, the result included, is.
        """
        provisional = False
        step = _ONE_DAY if n >= 0 else -_ONE_DAY
        remaining = abs(n)
        day = start
        scanned = 0
        while remaining:
            scanned += 1
            if scanned > MAX_SCAN_DAYS:
                raise CalendarError(
                    "calendar_scan_limit",
                    f"No {abs(n)} working days within {MAX_SCAN_DAYS} days of {start.isoformat()}",
                )
            try:
                day = day + step
            except OverflowError as exc:
                raise CalendarError(
                    "calendar_scan_limit", "The result is outside the date range"
                ) from exc
            working, marked = self._day(day)
            provisional = provisional or marked
            if working:
                remaining -= 1
        return DateAnswer(day, provisional)

    def workdays_between(self, a: date, b: date) -> CountAnswer:
        """Working days in ``(a, b]``; negative when ``b`` is before ``a``.

        The count ``add_workdays`` needs: for a working day ``b`` after ``a``,
        ``add_workdays(a, workdays_between(a, b))`` is ``b``. Provisional when
        any counted day is.
        """
        if b < a:
            answer = self.workdays_between(b, a)
            return CountAnswer(-answer.value, answer.provisional)
        if (b - a).days > MAX_SCAN_DAYS:
            raise CalendarError("calendar_scan_limit", f"The span exceeds {MAX_SCAN_DAYS} days")
        provisional = False
        count = 0
        day = a
        while day < b:
            day += _ONE_DAY
            working, marked = self._day(day)
            provisional = provisional or marked
            count += working
        return CountAnswer(count, provisional)

    # --- timestamps, as cal.* sees them ---------------------------------------

    def local_date(self, ts: datetime) -> date:
        """The calendar day of an instant in the calendar's timezone."""
        return _aware(ts).astimezone(self.zone).date()

    def is_workday_at(self, ts: datetime) -> DayAnswer:
        return self.is_workday(self.local_date(ts))

    def add_workdays_at(self, ts: datetime, n: int) -> TimestampAnswer:
        """``cal.addWorkdays``: the same wall-clock time ``n`` working days away."""
        local = _aware(ts).astimezone(self.zone)
        moved = self.add_workdays(local.date(), n)
        wall = datetime.combine(moved.value, local.time(), self.zone)
        return TimestampAnswer(wall.astimezone(ts.tzinfo), moved.provisional)

    def workdays_between_at(self, a: datetime, b: datetime) -> CountAnswer:
        return self.workdays_between(self.local_date(a), self.local_date(b))


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise CalendarError("naive_timestamp", "A timestamp must carry its UTC offset")
    return ts


def normalized_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The spec in the order that does not matter: years by number, dates ascending.

    Hashing this form keeps a package that only reorders its lists from
    publishing a new calendar version.
    """
    body = dict(spec)
    years = []
    for raw in sorted(spec["years"], key=lambda item: item["year"]):
        entry = dict(raw)
        for field in ("holidays", "workdays", "shortDays"):
            if field in entry and entry[field] is not None:
                entry[field] = sorted(entry[field])
        years.append(entry)
    body["years"] = years
    if body.get("weekend") is not None:
        body["weekend"] = sorted(body["weekend"])
    return body
