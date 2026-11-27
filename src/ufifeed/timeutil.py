from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def local_slot(day: date, clock: str, timezone: str) -> datetime:
    """First fold on fall-back; first valid minute after a spring-forward gap."""
    zone = ZoneInfo(timezone)
    naive = datetime.combine(day, time.fromisoformat(clock))
    for _ in range(24 * 60 + 1):
        candidate = naive.replace(tzinfo=zone, fold=0)
        result = candidate.astimezone(UTC)
        if result.astimezone(zone).replace(tzinfo=None) == naive:
            return result
        naive += timedelta(minutes=1)
    raise ValueError("Cannot resolve local schedule time")


def calendar_window(
    day: date, period: Literal["day", "week"], timezone: str, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve local calendar dates to UTC bounds, including daylight-saving changes."""
    if day > now.astimezone(ZoneInfo(timezone)).date():
        raise ValueError("Digest date is in the future")
    if period == "week":
        first = day - timedelta(days=day.weekday())
        length = 7
    else:
        first = day
        length = 1
    start = local_slot(first, "00:00", timezone)
    end = min(now, local_slot(first + timedelta(days=length), "00:00", timezone))
    if end <= start:
        raise ValueError("No time has elapsed in this period")
    return start, end


def latest_slot(now: datetime, times: list[str], timezone: str) -> datetime:
    today = now.astimezone(ZoneInfo(timezone)).date()
    slots = {
        local_slot(today - timedelta(days=offset), clock, timezone)
        for offset in range(3)
        for clock in times
    }
    return max(slot for slot in slots if slot <= now)


def next_slot(now: datetime, times: list[str], timezone: str) -> datetime:
    today = now.astimezone(ZoneInfo(timezone)).date()
    slots = {
        local_slot(today + timedelta(days=offset), clock, timezone)
        for offset in range(3)
        for clock in times
    }
    return min(slot for slot in slots if slot > now)
