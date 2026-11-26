from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
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
