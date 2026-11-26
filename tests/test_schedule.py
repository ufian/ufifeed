from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ufifeed.digest import Digests
from ufifeed.timeutil import latest_slot, local_slot, next_slot, stamp


def test_seattle_dst_gap_and_repeated_hour():
    zone = "America/Los_Angeles"
    assert local_slot(date(2026, 3, 8), "02:30", zone) == datetime(2026, 3, 8, 10, tzinfo=UTC)
    assert local_slot(date(2026, 11, 1), "01:30", zone) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    second_fold = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    assert latest_slot(second_fold, ["01:30"], zone).hour == 8
    assert next_slot(second_fold, ["01:30"], zone).day == 2


async def test_failed_collection_does_not_advance_window(store, config, credentials, monkeypatch):
    now = datetime(2026, 11, 25, 20, tzinfo=UTC)
    monkeypatch.setattr("ufifeed.digest.utcnow", lambda: now)
    store.set("started", True)
    store.set("schedule", {"enabled": True, "times": ["09:00"], "timezone": "America/Los_Angeles"})
    start = now - timedelta(days=3)
    store.set("window_end", stamp(start))
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=False))
    digests = Digests(config, credentials, store, reader)
    await digests.scheduled()
    assert store.last_window_end() == start
    assert store.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    reader.sync_all.return_value = True
    await digests.scheduled()
    await digests.scheduled()
    assert store.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert store.last_window_end() == datetime(2026, 11, 25, 17, tzinfo=UTC)


async def test_pause_during_sync_prevents_sending(store, config, credentials, monkeypatch):
    now = datetime(2026, 11, 25, 20, tzinfo=UTC)
    monkeypatch.setattr("ufifeed.digest.utcnow", lambda: now)
    store.set("started", True)
    store.set("window_end", stamp(now - timedelta(days=1)))
    store.set("schedule", {"enabled": True, "times": ["09:00"], "timezone": "America/Los_Angeles"})

    async def pause():
        schedule = store.get("schedule")
        schedule["enabled"] = False
        store.set("schedule", schedule)
        return True

    digests = Digests(config, credentials, store, SimpleNamespace(sync_all=pause))
    await digests.scheduled()
    assert not store.delivery_status()


async def test_schedule_reset_during_sync_cannot_move_window_backwards(
    store,
    config,
    credentials,
    monkeypatch,
):
    now = datetime(2026, 11, 25, 20, tzinfo=UTC)
    monkeypatch.setattr("ufifeed.digest.utcnow", lambda: now)
    store.set("started", True)
    store.set("window_end", stamp(now - timedelta(days=1)))
    store.set("schedule", {"enabled": True, "times": ["09:00"], "timezone": "America/Los_Angeles"})

    async def reset():
        store.set("window_end", stamp(now))
        return True

    await Digests(config, credentials, store, SimpleNamespace(sync_all=reset)).scheduled()
    assert store.last_window_end() == now
    assert store.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
