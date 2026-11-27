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


async def test_persisted_flood_pause_prevents_requests_after_restart(store, config, credentials):
    from ufifeed.timeutil import stamp

    store.set("folder_id", 214)
    store.set("reader_retry_at", stamp(utcnow() + timedelta(seconds=60)))
    reader = Reader(config, credentials, store)
    reader.refresh_folder = AsyncMock()
    reader.reconcile = AsyncMock()
    assert not await reader.sync_all()
    reader.refresh_folder.assert_not_awaited()
    store.set("reader_retry_at", stamp(utcnow() - timedelta(seconds=1)))
    assert await reader.sync_all()
    reader.refresh_folder.assert_awaited_once()
    assert store.get("reader_retry_at") is None


async def test_concurrent_collection_requests_share_the_completed_pass(store, config, credentials):
    import asyncio

    store.set("folder_id", 214)
    reader = Reader(config, credentials, store)
    gate = asyncio.Event()

    async def refresh():
        await gate.wait()

    async def reconcile(source):
        store.synced(source["id"], utcnow())

    reader.refresh_folder = AsyncMock(side_effect=refresh)
    reader.reconcile = AsyncMock(side_effect=reconcile)
    first = asyncio.create_task(reader.sync_all())
    await asyncio.sleep(0)
    second = asyncio.create_task(reader.sync_all())
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(first, second) == [True, True]
    reader.reconcile.assert_awaited_once()


async def test_forwarded_public_channel_is_joined_muted_and_added(store, config, credentials):
    from telethon import functions
    from telethon.tl import types

    from ufifeed.reader import Reader
    from ufifeed.timeutil import utcnow

    store.set("folder_id", 214)
    reader = Reader(config, credentials, store)
    existing = types.InputPeerChannel(999, 1)
    folder = types.DialogFilter(214, types.TextWithEntities("Ufifeed", []), [], [existing], [])
    reader.folders = AsyncMock(return_value=[{"id": 214, "filter": folder}])
    entity = types.Channel(
        123,
        "Forwarded",
        types.ChatPhotoEmpty(),
        utcnow(),
        left=True,
        broadcast=True,
        username="forwarded",
        access_hash=42,
    )
    reader.client = AsyncMock(return_value=True)
    reader.client.get_entity.return_value = entity
    response = await reader.add_forwarded_channel(-1000000000123, "forwarded")
    assert "Notifications are muted" in response
    requests = [call.args[0] for call in reader.client.await_args_list]
    assert [type(r) for r in requests] == [
        functions.channels.JoinChannelRequest,
        functions.account.UpdateNotifySettingsRequest,
        functions.messages.UpdateDialogFilterRequest,
    ]
    assert requests[1].settings.mute_until.year == 2038
    assert requests[2].filter.include_peers == [existing, types.InputPeerChannel(123, 42)]
    assert reader.wake.is_set()
    entity.left = False
    await reader.add_forwarded_channel(-1000000000123, "forwarded")
    assert reader.client.await_count == 3
