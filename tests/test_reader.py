from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from ufifeed.reader import Reader, ReaderError, folder_peers, message_data
from ufifeed.timeutil import utcnow


def make_message(msg_id, date, text="post", **kwargs):
    return types.Message(
        id=msg_id, peer_id=types.PeerChannel(123), date=date, message=text, **kwargs
    )


def test_folder_pins_exclusions_and_duplicates():
    a, b = types.InputPeerChannel(123, 1), types.InputPeerChannel(456, 2)
    folder = types.DialogFilter(2, types.TextWithEntities("Read", []), [a], [a, b], [b])
    assert folder_peers(folder) == [a]
    folder.broadcasts = True
    with pytest.raises(ReaderError):
        folder_peers(folder)


def test_protected_and_disappearing_messages_are_not_collected():
    assert message_data(make_message(1, utcnow(), noforwards=True)) is None
    assert message_data(make_message(2, utcnow(), ttl_period=60)) is None
    assert message_data(make_message(3, utcnow(), text=""))["text"] == ""


async def test_empty_initial_window_never_imports_old_history_on_second_sync(
    store,
    config,
    credentials,
):
    reader = Reader(config, credentials, store)
    old = make_message(50, utcnow() - timedelta(days=10))
    calls = []

    async def history(entity, **kwargs):
        calls.append(kwargs)
        if kwargs.get("limit") == 100:
            yield old

    reader.entities[-1000000000123] = SimpleNamespace(noforwards=False)
    reader.client = SimpleNamespace(
        get_messages=AsyncMock(return_value=[old]), iter_messages=history
    )
    await reader.reconcile(store.sources()[0])
    await reader.reconcile(store.sources()[0])
    assert store.sources()[0]["cursor"] == 50
    assert "offset_date" in calls[0]
    assert calls[2]["min_id"] == 50
    assert store.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_live_event_during_backfill_does_not_skip_middle_messages(store, config, credentials):
    reader = Reader(config, credentials, store)
    date = utcnow() - timedelta(minutes=5)
    first, second, live = [make_message(i, date, str(i)) for i in (10, 20, 100)]

    async def history(entity, **kwargs):
        if kwargs.get("limit") == 100:
            return
        yield first
        await reader.on_message(SimpleNamespace(chat_id=-1000000000123, message=live, id=100))
        yield second

    reader.entities[-1000000000123] = SimpleNamespace(noforwards=False)

    async def get_messages(entity, *, ids=None, **kwargs):
        return (
            [{m.id: m for m in (first, second, live)}[msg_id] for msg_id in ids]
            if ids
            else [second]
        )

    reader.client = SimpleNamespace(get_messages=get_messages, iter_messages=history)
    await reader.reconcile(store.sources()[0])
    assert store.sources()[0]["cursor"] == 20
    assert store.conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3


async def test_changed_folder_requires_its_own_successful_sync(store, config, credentials):
    reader = Reader(config, credentials, store)
    store.change_folder(3)
    # Simulate an in-flight refresh for the previous folder noticing the change and returning.
    reader.refresh_folder = AsyncMock(return_value=None)
    assert not await reader.sync_all()
