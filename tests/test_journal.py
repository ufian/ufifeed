import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from telethon import types

from ufifeed.reader import Reader, message_data
from ufifeed.store import SCHEMA, Store
from ufifeed.timeutil import stamp, utcnow

SOURCE = -1000000000123


def test_edit_history_survives_deletion_and_stale_events(store, item):
    store.upsert_live(SOURCE, item)
    changed = {**item, "text": "Corrected claim", "edited_at": utcnow()}
    store.upsert_live(SOURCE, changed)
    store.upsert_live(SOURCE, changed)
    store.delete_messages(SOURCE, [item["id"]])
    store.delete_messages(SOURCE, [item["id"]])
    store.upsert_live(SOURCE, item)  # Delayed original update cannot undo the correction.
    store.upsert_live(SOURCE, changed)  # Nor resurrect a deleted message.
    history = store.message_history(SOURCE, item["id"])
    assert [e["kind"] for e in history] == ["created", "edited", "deleted"]
    assert history[0]["payload"]["text"] == item["text"]
    assert history[1]["payload"]["text"] == "Corrected claim"
    assert history[1]["payload"]["edited_at"] == stamp(changed["edited_at"])
    row = store.candidates(item["date"], utcnow())[0]
    assert row["deleted"] and row["text"] == "Corrected claim"
    assert all(
        b"claim" not in row[0] for row in store.conn.execute("SELECT payload FROM message_events")
    )


def test_unseen_deletion_tombstone_and_late_content(store, item):
    store.delete_messages(SOURCE, [item["id"]])
    store.upsert_live(SOURCE, item)
    row = store.candidates(item["date"], utcnow())[0]
    assert row["deleted"] and row["text"] == item["text"]
    assert row["date"] == stamp(item["date"])
    assert not store.message_history(SOURCE, item["id"])[1]["payload"]["content_observed"]


def test_old_post_deleted_now_enters_current_window(store, item):
    old = {**item, "date": utcnow() - timedelta(days=10)}
    store.upsert_live(SOURCE, old)
    start = utcnow()
    store.delete_messages(SOURCE, [old["id"]])
    assert store.candidates(start, utcnow())[0]["text"] == old["text"]


def test_unlimited_archive_preserves_versions_but_expires_derived_output(store, item):
    store.upsert_live(SOURCE, item)
    store.enqueue("test", item["date"], utcnow(), "ai", ["summary"], advance=False)
    store.cache_put("test", [{"text": "cached"}])
    store.cleanup(0, utcnow() + timedelta(days=365))
    assert store.journal_stats() == {"messages": 1, "deleted": 0, "events": 1}
    assert store.cache_get("test") is None
    assert store.delivery_status() == {"expired": 1}
    store.purge(SOURCE)
    assert store.journal_stats()["events"] == 0


def test_v1_migration_preserves_existing_encrypted_text_and_baseline(config, item):
    key = Fernet.generate_key()
    config.storage.key_path.write_bytes(key)
    cipher = Fernet(key)
    config.storage.database_path.parent.mkdir()
    with sqlite3.connect(config.storage.database_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema_version','1')")
        conn.execute(
            "INSERT INTO meta VALUES ('key_check',?)",
            (json.dumps(cipher.encrypt(b"ufifeed").decode()),),
        )
        conn.execute(
            "INSERT INTO sources(id,title,since) VALUES (?,?,?)",
            (SOURCE, "Source", stamp(item["date"])),
        )
        conn.execute(
            "INSERT INTO messages(source_id,id,date,text,content_hash) VALUES (?,?,?,?,?)",
            (SOURCE, item["id"], stamp(item["date"]), cipher.encrypt(item["text"].encode()), "old"),
        )
    db = Store.open(config)
    assert db.get("schema_version") == 3
    assert db.candidates(item["date"], utcnow())[0]["text"] == item["text"]
    assert db.message_history(SOURCE, item["id"])[0]["kind"] == "baseline"
    db.close()
    reopened = Store.open(config)
    assert reopened.journal_stats()["events"] == 1
    reopened.close()


def test_media_and_reaction_counts_are_captured_without_downloading():
    message = types.Message(
        id=1,
        peer_id=types.PeerChannel(123),
        from_id=types.PeerUser(42),
        date=utcnow(),
        message="",
        media=types.MessageMediaPhoto(),
        reactions=types.MessageReactions(
            results=[
                types.ReactionCount(types.ReactionEmoji("👍"), 17),
                types.ReactionCount(types.ReactionPaid(), 100),
            ]
        ),
    )
    data = message_data(message)
    assert data["text"] == "" and data["media_kind"] == "MessageMediaPhoto"
    assert data["reaction_count"] == 17 and data["paid_reactions"] == 100
    assert data["sender_id"] == 42


async def test_raw_reactions_append_history_once_and_keep_text(store, config, credentials, item):
    store.upsert_live(SOURCE, item)
    reader = Reader(config, credentials, store)
    update = types.UpdateMessageReactions(
        types.PeerChannel(123),
        item["id"],
        types.MessageReactions(results=[types.ReactionCount(types.ReactionEmoji("👍"), 19)]),
    )
    await reader.on_raw(update)
    await reader.on_raw(update)
    assert [e["kind"] for e in store.message_history(SOURCE, item["id"])] == [
        "created",
        "reactions",
    ]
    row = store.candidates(item["date"], utcnow())[0]
    assert row["reaction_count"] == 19 and row["text"] == item["text"]


async def test_unscoped_deletion_cannot_delete_colliding_channel_ids(
    store, config, credentials, item
):
    store.sync_folder(
        [
            {"id": SOURCE, "title": "Channel"},
            {"id": -42, "title": "Group", "kind": "group", "peer_type": "chat"},
        ],
        item["date"],
    )
    store.upsert_live(SOURCE, item)
    store.upsert_live(-42, item)
    reader = Reader(config, credentials, store)
    await reader.on_delete(SimpleNamespace(chat_id=None, deleted_ids=[item["id"]]))
    rows = {row["source_id"]: row for row in store.candidates(item["date"], utcnow())}
    assert rows[-42]["deleted"] and not rows[SOURCE]["deleted"]


async def test_explicit_verification_detects_missed_deletion(store, config, credentials, item):
    store.upsert_live(SOURCE, item)
    reader = Reader(config, credentials, store)
    reader.entities[SOURCE] = SimpleNamespace(noforwards=False)

    async def empty(*args, **kwargs):
        if False:
            yield

    reader.client = SimpleNamespace(
        get_messages=AsyncMock(side_effect=[[], [None]]), iter_messages=empty
    )
    await reader.reconcile(store.sources()[0])
    assert store.journal_stats()["deleted"] == 1
    assert store.candidates(item["date"], utcnow())[0]["text"] == item["text"]


def test_message_window_has_no_5000_message_cap(store, item):
    store.commit_page(SOURCE, [{**item, "id": i} for i in range(5001)], 5000)
    assert len(store.candidates(item["date"], utcnow())) == 5001


def test_reaction_changes_do_not_collapse_when_counts_return_to_previous_value(store, item):
    for count in (5, 6, 5):
        store.upsert_live(
            SOURCE,
            {**item, "reactions": [{"emoji": "👍", "count": count}], "reaction_count": count},
        )
    events = store.message_history(SOURCE, item["id"])
    assert [e["payload"]["reaction_count"] for e in events] == [5, 6, 5]


def test_repeated_text_with_new_edit_timestamp_is_a_version(store, item):
    store.upsert_live(SOURCE, item)
    store.upsert_live(SOURCE, {**item, "edited_at": utcnow()})
    assert [e["kind"] for e in store.message_history(SOURCE, item["id"])] == ["created", "edited"]


async def test_incomplete_verification_does_not_invent_deletions(store, config, credentials, item):
    import pytest

    from ufifeed.reader import ReaderError

    store.upsert_live(SOURCE, item)
    reader = Reader(config, credentials, store)
    reader.entities[SOURCE] = SimpleNamespace(noforwards=False)

    async def empty(*args, **kwargs):
        if False:
            yield

    reader.client = SimpleNamespace(get_messages=AsyncMock(return_value=[]), iter_messages=empty)
    with pytest.raises(ReaderError, match="Incomplete"):
        await reader.reconcile(store.sources()[0])
    assert store.journal_stats()["deleted"] == 0
    assert store.get(f"verify:{SOURCE}", 0) == 0


async def test_channel_service_events_are_journaled_live(store, config, credentials):
    reader = Reader(config, credentials, store)
    message = types.MessageService(
        id=12, peer_id=types.PeerChannel(123), date=utcnow(), action=types.MessageActionPinMessage()
    )
    await reader.on_raw(types.UpdateNewChannelMessage(message, pts=1, pts_count=1))
    assert store.journal_stats()["messages"] == 1
    assert (
        store.message_history(SOURCE, 12)[0]["payload"]["media_kind"] == "MessageActionPinMessage"
    )


def test_reconciliation_does_not_rewrite_unchanged_ciphertext(store, item):
    store.upsert_live(SOURCE, item)
    before = store.conn.total_changes
    store.refresh_page(SOURCE, [item] * 200)
    assert store.conn.total_changes == before
    assert store.sources()[0]["cursor"] == 0
    assert store.journal_stats()["events"] == 1
