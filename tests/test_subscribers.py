from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ufifeed.bot import deliver_one
from ufifeed.digest import Digests
from ufifeed.timeutil import stamp, utcnow

SOURCE = -1000000000123
OTHER = -1000000000456


def test_invites_are_single_use_and_preferences_cannot_expand_access(store):
    store.sync_folder(
        [{"id": SOURCE, "title": "First"}, {"id": OTHER, "title": "Second"}], utcnow()
    )
    code = store.create_invite({SOURCE})
    assert store.join(456, code)
    assert not store.join(789, code)
    assert store.selected_sources(456) == {SOURCE}
    with pytest.raises(ValueError):
        store.select_sources(456, {OTHER})
    store.select_sources(456, set())
    assert store.selected_sources(456) == set()
    assert store.selected_sources(None) == {SOURCE, OTHER}
    expired = store.create_invite({OTHER})
    with store.conn:
        store.conn.execute("UPDATE invites SET expires_at=?", (stamp(utcnow()),))
    assert not store.join(789, expired)


async def test_scheduled_digests_are_scoped_and_delivered_to_each_subscriber(
    store, config, credentials, item, monkeypatch
):
    now = utcnow()
    store.sync_folder([{"id": SOURCE, "title": "First"}, {"id": OTHER, "title": "Second"}], now)
    assert store.join(456, store.create_invite({SOURCE, OTHER}))
    assert store.join(789, store.create_invite({OTHER}))
    store.select_sources(456, {SOURCE})
    store.select_sources(None, {OTHER})
    store.set("started", True)
    store.set("schedule", {"enabled": True, "times": ["09:00"], "timezone": "UTC"})
    store.upsert_live(SOURCE, {**item, "date": now - timedelta(minutes=1), "text": "Older story"})
    store.upsert_live(
        SOURCE, {**item, "id": 100, "date": now + timedelta(seconds=1), "text": "Red story"}
    )
    store.upsert_live(OTHER, {**item, "date": now + timedelta(seconds=1), "text": "Blue story"})
    due = now + timedelta(minutes=1)
    monkeypatch.setattr("ufifeed.digest.latest_slot", lambda *args: due)
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=True))
    digests = Digests(config, credentials, store, reader)
    await digests.scheduled()
    await digests.scheduled()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    while await deliver_one(bot, store, config.owner.user_id):
        pass
    deliveries = {call.args[0]: call.args[1] for call in bot.send_message.await_args_list}
    assert bot.send_message.await_count == 3
    assert "Red story" in deliveries[456] and "Blue story" not in deliveries[456]
    assert "Older story" not in deliveries[456]
    assert "Blue story" in deliveries[789] and "Red story" not in deliveries[789]
    assert "Blue story" in deliveries[config.owner.user_id]
    assert store.subscriber(456)["window_end"] == stamp(due)


async def test_revocation_expires_queued_subscriber_digest(store, config, item):
    assert store.join(456, store.create_invite({SOURCE}))
    store.set("started", True)
    store.enqueue(
        "guest", item["date"], utcnow(), "extractive", ["private"], advance=False, recipient_id=456
    )
    assert store.revoke(456)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await deliver_one(bot, store, config.owner.user_id)
    bot.send_message.assert_not_awaited()
    assert store.delivery_status() == {"expired": 1}
