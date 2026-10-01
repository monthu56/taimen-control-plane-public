"""Working-day calendars: the arithmetic behind ``cal.*`` (CP-ADR-0074 §9, CP-ADR-0075).

A calendar is a timezone, the ISO weekdays that are weekend, and per
published year its holidays, the working days moved onto a weekend and the
shortened days. A day of a published year is a working day when it is a moved
working day, otherwise when it is neither a holiday nor a weekend day. A day
outside every published year knows only the weekend.

A calendar may also declare ``workingHours`` (CP-ADR-0078 §2): the intervals
of a working day in the calendar's local time, optionally other intervals for
some ISO weekdays, and how much shorter a short day is (taken off the end of
its last interval). A moved working day has the plain intervals. Working time
is measured on the local wall clock of the calendar.

Every answer carries ``provisional``: it is true when the computation looked
at a day of a year marked ``provisional`` or of a year the calendar does not
publish at all. A deadline then reads "provisional" until a confirmed year
replaces the guess (a new calendar version reschedules the timers).

Pure functions over plain values: the engine, the package tests and replay
compute the same answer from the same calendar version.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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


@dataclass(frozen=True)
class DurationAnswer:
    value: timedelta
    provisional: bool


# Minutes from local midnight: [start, end), end up to 24:00.
Interval = tuple[int, int]
_MINUTES_PER_DAY = 24 * 60
_MINUTE = timedelta(minutes=1)
_CLOCK = re.compile(r"(?P<hours>\d{2}):(?P<minutes>\d{2})")
_REDUCTION = re.compile(
    r"P(?!$)(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?"
)


def _clock(text: Any, *, path: str) -> int:
    match = _CLOCK.fullmatch(str(text))
    if match is not None and int(match["minutes"]) < 60:
        value = int(match["hours"]) * 60 + int(match["minutes"])
        if value <= _MINUTES_PER_DAY:
            return value
    raise CalendarError("invalid_working_hours", f"{text!r} is not a time of day", path=path)


def _intervals(raw: Iterable[Mapping[str, Any]], *, path: str) -> tuple[Interval, ...]:
    found: list[Interval] = []
    for index, item in enumerate(raw):
        start = _clock(item["from"], path=f"{path}/{index}/from")
        end = _clock(item["to"], path=f"{path}/{index}/to")
        if start >= _MINUTES_PER_DAY or end <= start:
            raise CalendarError(
                "invalid_working_hours",
                f"The interval {item['from']}-{item['to']} does not end after it starts",
                path=f"{path}/{index}",
            )
        if found and start < found[-1][1]:
            raise CalendarError(
                "invalid_working_hours",
                f"The interval {item['from']}-{item['to']} starts before the previous one ends",
                path=f"{path}/{index}",
            )
        found.append((start, end))
    return tuple(found)


def _reduction(text: str, *, path: str) -> int:
    match = _REDUCTION.fullmatch(text)
    if match is None:
        raise CalendarError(
            "invalid_working_hours",
            f"{text!r} is not a duration of weeks, days, hours, minutes and seconds",
            path=path,
        )
    parts = {key: int(value) for key, value in match.groupdict().items() if value}
    length = timedelta(**parts)
    if length % _MINUTE:
        raise CalendarError(
            "invalid_working_hours", f"{text!r} is not a whole number of minutes", path=path
        )
    return length // _MINUTE


@dataclass(frozen=True)
class WorkingHours:
    """``workingHours`` of a calendar: the working intervals of a working day.

    ``weekdays`` replaces ``intervals`` for an ISO weekday (an empty tuple: no
    working hours that weekday); a moved working day keeps ``intervals``.
    ``short_day_reduction`` minutes come off the end of a short day, from its
    last interval backwards.
    """

    intervals: tuple[Interval, ...]
    weekdays: Mapping[int, tuple[Interval, ...]]
    short_day_reduction: int = 0

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any], *, path: str) -> "WorkingHours":
        intervals = _intervals(spec["intervals"], path=f"{path}/intervals")
        if not intervals:
            raise CalendarError(
                "invalid_working_hours", "A working day needs an interval", path=f"{path}/intervals"
            )
        weekdays: dict[int, tuple[Interval, ...]] = {}
        for key, raw in (spec.get("weekdays") or {}).items():
            weekday = int(key) if str(key) in {"1", "2", "3", "4", "5", "6", "7"} else 0
            if not weekday:
                raise CalendarError(
                    "invalid_working_hours",
                    f"{key!r} is not an ISO weekday",
                    path=f"{path}/weekdays/{key}",
                )
            weekdays[weekday] = _intervals(raw, path=f"{path}/weekdays/{key}")
        raw_reduction = spec.get("shortDayReduction")
        reduction = (
            0
            if raw_reduction is None
            else _reduction(raw_reduction, path=f"{path}/shortDayReduction")
        )
        return cls(intervals=intervals, weekdays=weekdays, short_day_reduction=reduction)

    def of_day(self, weekday: int, *, moved: bool, short: bool) -> tuple[Interval, ...]:
        """The intervals of a working day with this ISO weekday."""
        intervals = self.intervals if moved else self.weekdays.get(weekday, self.intervals)
        if not short or not self.short_day_reduction:
            return intervals
        cut = self.short_day_reduction
        kept: list[Interval] = []
        for start, end in reversed(intervals):
            if cut >= end - start:
                cut -= end - start
                continue
            kept.append((start, end - cut))
            cut = 0
        return tuple(reversed(kept))


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
    working_hours: WorkingHours | None = None

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "Calendar":
        """A calendar from ``$defs.calendarSpec`` (camelCase, dates as ISO strings).

        The shape is the schema's business; this checks what the schema
        cannot: a known timezone, one entry per year, dates inside their year,
        no day both a holiday and a moved working day, working intervals in
        order and without overlaps.
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
        raw_hours = spec.get("workingHours")
        working_hours = (
            None
            if raw_hours is None
            else WorkingHours.from_spec(raw_hours, path="/spec/workingHours")
        )
        return cls(timezone=timezone, weekend=weekend, years=years, working_hours=working_hours)

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

    # --- working time (CP-ADR-0078 §2) ------------------------------------------

    def _hours(self) -> WorkingHours:
        if self.working_hours is None:
            raise CalendarError(
                "calendar_without_hours", "The calendar does not declare working hours"
            )
        return self.working_hours

    def working_intervals(self, day: date) -> tuple[tuple[Interval, ...], bool]:
        """The working intervals of a local day and whether they are provisional.

        A day of a year the calendar does not publish has the intervals of
        its weekday when it is not a weekend day, and is never short.
        """
        hours = self._hours()
        working, provisional = self._day(day)
        if not working:
            return (), provisional
        entry = self.years.get(day.year)
        moved = entry is not None and day in entry.workdays
        short = entry is not None and day in entry.short_days
        return hours.of_day(day.isoweekday(), moved=moved, short=short), provisional

    def add_working_time(self, start: datetime, amount: timedelta) -> TimestampAnswer:
        """The local wall-clock moment ``amount`` of working time after ``start``.

        ``start`` and the answer are naive local times of the calendar. From
        outside working hours the count starts at the next working interval;
        an amount that uses up an interval ends at its end, not at the start
        of the next one. A negative ``amount`` counts back the same way; zero
        is ``start`` as it is. Provisional when any day it looked at is.
        """
        _local(start)
        if not amount:
            return TimestampAnswer(start, False)
        forward = amount > timedelta(0)
        remaining = abs(amount)
        day = start.date()
        provisional = False
        for _ in range(MAX_SCAN_DAYS):
            intervals, marked = self.working_intervals(day)
            provisional = provisional or marked
            midnight = datetime.combine(day, time())
            spans = [(midnight + s * _MINUTE, midnight + e * _MINUTE) for s, e in intervals]
            for low, high in spans if forward else reversed(spans):
                if forward:
                    low = max(low, start)
                    if low < high:
                        if remaining <= high - low:
                            return TimestampAnswer(low + remaining, provisional)
                        remaining -= high - low
                else:
                    high = min(high, start)
                    if low < high:
                        if remaining <= high - low:
                            return TimestampAnswer(high - remaining, provisional)
                        remaining -= high - low
            try:
                day = day + (_ONE_DAY if forward else -_ONE_DAY)
            except OverflowError as exc:
                raise CalendarError(
                    "calendar_scan_limit", "The result is outside the date range"
                ) from exc
        raise CalendarError(
            "calendar_scan_limit",
            f"No {abs(amount)} of working time within {MAX_SCAN_DAYS} days of {start.isoformat()}",
        )

    def working_time_between(self, a: datetime, b: datetime) -> DurationAnswer:
        """Working time in ``[a, b]`` of naive local times; negative when ``b`` is before ``a``.

        For ``b`` inside working hours, ``add_working_time(a,
        working_time_between(a, b))`` is ``b``. Provisional when any day of
        the span is.
        """
        _local(a)
        _local(b)
        if b < a:
            answer = self.working_time_between(b, a)
            return DurationAnswer(-answer.value, answer.provisional)
        if (b.date() - a.date()).days > MAX_SCAN_DAYS:
            raise CalendarError("calendar_scan_limit", f"The span exceeds {MAX_SCAN_DAYS} days")
        total = timedelta(0)
        provisional = False
        day = a.date()
        while day <= b.date():
            intervals, marked = self.working_intervals(day)
            provisional = provisional or marked
            midnight = datetime.combine(day, time())
            for s, e in intervals:
                low = max(midnight + s * _MINUTE, a)
                high = min(midnight + e * _MINUTE, b)
                if low < high:
                    total += high - low
            day += _ONE_DAY
        return DurationAnswer(total, provisional)

    def next_working_time(self, start: datetime) -> TimestampAnswer:
        """``start`` inside working hours, otherwise the start of the next working interval.

        ``start`` and the answer are naive local times of the calendar; the
        end of an interval is outside it. A ``workdays`` deadline counted from
        outside working hours starts here (CP-ADR-0078 §1).
        """
        _local(start)
        day = start.date()
        provisional = False
        for _ in range(MAX_SCAN_DAYS):
            intervals, marked = self.working_intervals(day)
            provisional = provisional or marked
            midnight = datetime.combine(day, time())
            for s, e in intervals:
                low, high = midnight + s * _MINUTE, midnight + e * _MINUTE
                if start < high:
                    return TimestampAnswer(max(low, start), provisional)
            try:
                day = day + _ONE_DAY
            except OverflowError as exc:
                raise CalendarError(
                    "calendar_scan_limit", "The result is outside the date range"
                ) from exc
        raise CalendarError(
            "calendar_scan_limit",
            f"No working time within {MAX_SCAN_DAYS} days of {start.isoformat()}",
        )

    def _wall(self, ts: datetime) -> datetime:
        return _aware(ts).astimezone(self.zone).replace(tzinfo=None)

    def next_working_time_at(self, ts: datetime) -> TimestampAnswer:
        """:meth:`next_working_time` of an instant, in the offset of ``ts``."""
        moved = self.next_working_time(self._wall(ts))
        wall = moved.value.replace(tzinfo=self.zone)
        return TimestampAnswer(wall.astimezone(ts.tzinfo), moved.provisional)

    def add_working_time_at(self, ts: datetime, amount: timedelta) -> TimestampAnswer:
        """``cal.addWorkingTime``: the instant, in the offset of ``ts``."""
        moved = self.add_working_time(self._wall(ts), amount)
        wall = moved.value.replace(tzinfo=self.zone)
        return TimestampAnswer(wall.astimezone(ts.tzinfo), moved.provisional)

    def working_time_between_at(self, a: datetime, b: datetime) -> DurationAnswer:
        """``cal.workingTimeBetween`` of two instants."""
        return self.working_time_between(self._wall(a), self._wall(b))


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise CalendarError("naive_timestamp", "A timestamp must carry its UTC offset")
    return ts


def _local(ts: datetime) -> datetime:
    if ts.tzinfo is not None:
        raise CalendarError(
            "aware_local_time", "A local time of the calendar carries no UTC offset"
        )
    return ts


def normalized_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The spec in the order that does not matter: years by number, dates ascending.

    Hashing this form keeps a package that only reorders its lists from
    publishing a new calendar version. ``workingHours`` is part of the form
    only when the spec has it, so a calendar without it hashes as before
    CP-ADR-0078; its weekday keys are strings, as JSON has them.
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
    if body.get("workingHours") is None:
        body.pop("workingHours", None)
    elif body["workingHours"].get("weekdays") is not None:
        hours = dict(body["workingHours"])
        hours["weekdays"] = {str(key): value for key, value in hours["weekdays"].items()}
        body["workingHours"] = hours
    return body
