"""Slot arithmetic: weekly hours to bookable start times, minus days off and full slots.

Everything here is pure (no database) so it can be tested on its own. Weekly hours are
wall-clock times in the business's time zone; start times are returned in UTC.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
_HHMM = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")
MAX_RANGES_PER_DAY = 6


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def minutes(value: str) -> int:
    """ "09:30" -> 570. "24:00" is allowed as the end of a day."""
    match = _HHMM.match(value.strip())
    if not match:
        raise ValueError(f'"{value}" is not a time like 09:30.')
    hours, mins = int(match.group(1)), int(match.group(2))
    total = hours * 60 + mins
    if total > 24 * 60:
        raise ValueError(f'"{value}" is not a time like 09:30.')
    return total


def hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def clean_weekly_hours(data: dict[str, Any]) -> dict[str, list[list[str]]]:
    """Validate and normalise weekly hours: known weekdays, ranges that end after they
    start, sorted and not overlapping. Raises ValueError with a readable message."""
    out: dict[str, list[list[str]]] = {}
    for day, ranges in (data or {}).items():
        if day not in WEEKDAYS:
            raise ValueError(f'"{day}" is not a weekday. Use mon, tue, wed, thu, fri, sat or sun.')
        if not isinstance(ranges, list):
            raise ValueError(f"Hours for {WEEKDAY_NAMES[day]} must be a list of [start, end] pairs.")
        if len(ranges) > MAX_RANGES_PER_DAY:
            raise ValueError(f"{WEEKDAY_NAMES[day]} can have at most {MAX_RANGES_PER_DAY} time ranges.")
        parsed: list[tuple[int, int]] = []
        for pair in ranges:
            if not isinstance(pair, list | tuple) or len(pair) != 2:
                raise ValueError(f"Hours for {WEEKDAY_NAMES[day]} must be [start, end] pairs.")
            start, end = minutes(str(pair[0])), minutes(str(pair[1]))
            if end <= start:
                raise ValueError(f"On {WEEKDAY_NAMES[day]}, {pair[1]} must be after {pair[0]}.")
            parsed.append((start, end))
        parsed.sort()
        for (_, prev_end), (start, _) in itertools.pairwise(parsed):
            if start < prev_end:
                raise ValueError(f"On {WEEKDAY_NAMES[day]}, two time ranges overlap.")
        if parsed:
            out[day] = [[hhmm(s), hhmm(e)] for s, e in parsed]
    return {d: out[d] for d in WEEKDAYS if d in out}


def local_to_utc(day: date, total_minutes: int, tz: ZoneInfo) -> datetime:
    """A wall-clock time on a local day, in UTC. "24:00" is the next midnight."""
    if total_minutes >= 24 * 60:
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return datetime.combine(day, time(total_minutes // 60, total_minutes % 60), tzinfo=tz).astimezone(UTC)


def day_starts(weekly_hours: dict[str, Any], day: date, slot_minutes: int, tz: ZoneInfo) -> list[datetime]:
    """Every start time on this local day under the weekly hours, in UTC."""
    starts: list[datetime] = []
    for start_s, end_s in weekly_hours.get(WEEKDAYS[day.weekday()], []):
        start, end = minutes(start_s), minutes(end_s)
        m = start
        while m + slot_minutes <= end:
            starts.append(local_to_utc(day, m, tz))
            m += slot_minutes
    return starts


@dataclass(frozen=True)
class Busy:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class DayOff:
    resource_id: Any  # None means the whole business
    starts_on: date
    ends_on: date


def is_day_off(day: date, resource_id: Any, days_off: Iterable[DayOff]) -> bool:
    return any(
        (d.resource_id is None or d.resource_id == resource_id) and d.starts_on <= day <= d.ends_on
        for d in days_off
    )


def overlapping(start: datetime, end: datetime, busy: Sequence[Busy]) -> int:
    return sum(1 for b in busy if b.starts_at < end and b.ends_at > start)


def free_starts(
    *,
    weekly_hours: dict[str, Any],
    slot_minutes: int,
    capacity: int,
    day: date,
    tz: ZoneInfo,
    busy: Sequence[Busy],
    earliest: datetime,
    resource_id: Any = None,
    days_off: Iterable[DayOff] = (),
) -> list[datetime]:
    """Start times on this day that are open, not on a day off, not before `earliest`,
    and still have room."""
    if is_day_off(day, resource_id, days_off):
        return []
    length = timedelta(minutes=slot_minutes)
    return [
        s
        for s in day_starts(weekly_hours, day, slot_minutes, tz)
        if s >= earliest and overlapping(s, s + length, busy) < capacity
    ]


def runs(starts: Sequence[datetime], slot_minutes: int, tz: ZoneInfo) -> list[tuple[str, str]]:
    """Consecutive start times grouped as (first, last) local "HH:MM" pairs."""
    out: list[tuple[str, str]] = []
    step = timedelta(minutes=slot_minutes)
    first = prev = None
    for s in starts:
        if prev is not None and s - prev == step:
            prev = s
            continue
        if first is not None and prev is not None:
            out.append((first.astimezone(tz).strftime("%H:%M"), prev.astimezone(tz).strftime("%H:%M")))
        first = prev = s
    if first is not None and prev is not None:
        out.append((first.astimezone(tz).strftime("%H:%M"), prev.astimezone(tz).strftime("%H:%M")))
    return out


def describe_runs(pairs: Sequence[tuple[str, str]]) -> str:
    return ", ".join(a if a == b else f"{a}-{b}" for a, b in pairs)


def nice_date(value: date) -> str:
    """ "Mon 23 Nov 2026"."""
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b %Y')}"


def nice_time(value: datetime, tz: ZoneInfo) -> str:
    """ "10:30 AM"."""
    local = value.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_clock(value: str) -> int | None:
    """ "14:30" or "2:30 PM" -> minutes after midnight."""
    text = (value or "").strip().lower().replace(".", "")
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if not match:
        return None
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute
