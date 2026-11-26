from datetime import timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken

from ufifeed.store import Store
from ufifeed.timeutil import utcnow


def test_live_update_does_not_skip_history_and_edits_replace_text(store, item):
    live = {**item, "id": 100, "text": "live secret"}
    store.upsert_live(-1000000000123, live)
    assert store.sources()[0]["cursor"] == 0
    store.commit_page(-1000000000123, [item], 10)
    assert store.sources()[0]["cursor"] == 10
    store.commit_page(-1000000000123, [{**item, "text": "edited secret"}], 10)
    rows = store.candidates(item["date"] - timedelta(seconds=1), utcnow())
    assert len(rows) == 2
    assert {r["text"] for r in rows} == {"edited secret", "live secret"}
    assert all(
        isinstance(r[0], bytes) and b"secret" not in r[0]
        for r in store.conn.execute("SELECT text FROM messages")
    )


def test_outbox_survives_restart_and_unknown_blocks_later_parts(store, item):
    end, start = utcnow(), item["date"]
    store.enqueue("slot", start, end, "extractive", ["secret part1", "part2"], advance=True)
    claimed = store.claim()
    assert claimed["payload"] == "secret part1"
    store.recover_sending()
    assert store.claim() is None
    assert store.delivery_status() == {"unknown": 1, "pending": 1}
    assert store.retry(claimed["id"])
    retried = store.claim()
    store.delivered(retried["id"], 42)
    assert store.claim()["payload"] == "part2"
    assert store.enqueue("slot", start, end, "extractive", ["duplicate"], advance=True) is None
    assert store.last_window_end() == end


def test_remove_from_folder_stops_candidates_and_readd_bootstraps(store, item):
    store.upsert_live(-1000000000123, item)
    store.commit_page(-1000000000123, [], 100)
    store.sync_folder([], utcnow())
    assert not store.candidates(item["date"], utcnow())
    store.sync_folder([{"id": -1000000000123, "title": "Source"}], utcnow())
    assert store.sources()[0]["cursor"] == 0
    assert not store.sources()[0]["initialized"]


def test_retention_erases_content_but_keeps_idempotency_keys(store, item):
    store.upsert_live(-1000000000123, item)
    now = utcnow()
    store.enqueue("slot", item["date"], now, "extractive", ["secret"], advance=True)
    store.cleanup(1, now + timedelta(days=2))
    assert not store.candidates(item["date"], now)
    assert store.conn.execute("SELECT payload,status FROM outbox").fetchone()[:] == (b"", "expired")
    assert store.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert not store.retry(1)


def test_wrong_data_key_cannot_open_existing_db(store, config):
    with pytest.raises(InvalidToken):
        Store(config.storage.database_path, Fernet.generate_key())


def test_switching_folder_invalidates_queued_private_content(store, item):
    store.enqueue("slot", item["date"], utcnow(), "extractive", ["secret"], advance=False)
    store.change_folder(3)
    assert store.claim() is None
    assert store.delivery_status() == {"expired": 1}


def test_reopening_database_preserves_unsent_payload_and_settings(store, config, item):
    store.set("topics", ["important"])
    store.enqueue("slot", item["date"], utcnow(), "extractive", ["private"], advance=True)
    reopened = Store.open(config)
    try:
        reopened.bootstrap(config)
        assert reopened.get("topics") == ["important"]
        assert reopened.claim()["payload"] == "private"
    finally:
        reopened.close()
