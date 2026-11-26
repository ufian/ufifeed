from types import SimpleNamespace
from unittest.mock import AsyncMock

from ufifeed.cli import configure, status
from ufifeed.timeutil import utcnow


async def test_named_folder_bootstrap_queues_exactly_one_initial_digest(
    store,
    config,
    credentials,
    item,
    monkeypatch,
    capsys,
):
    class FakeReader:
        def __init__(self, config, credentials, database):
            self.store = database
            self.client = SimpleNamespace(disconnect=AsyncMock())

        connect = AsyncMock()
        folders = AsyncMock(return_value=[{"id": 4, "title": "Ufifeed"}])

        async def sync_all(self):
            self.store.sync_folder([{"id": -1000000000123, "title": "Source"}], item["date"])
            self.store.upsert_live(-1000000000123, item)
            self.store.set("folder_title", "Ufifeed")
            self.store.synced(-1000000000123, utcnow())
            return True

    monkeypatch.setattr("ufifeed.cli.Reader", FakeReader)
    args = SimpleNamespace(
        folder="Ufifeed",
        language="ru",
        mode="extractive",
        enable_ai_for_folder=False,
        queue_digest=True,
    )
    await configure(config, credentials, args)
    await configure(config, credentials, args)
    assert store.get("folder_id") == 4
    assert store.get("language") == "ru"
    assert store.get("started")
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert store.delivery_status() == {"pending": 1}
    capsys.readouterr()
    status(config)
    output = capsys.readouterr().out
    assert '"folder_title": "Ufifeed"' in output
    assert item["text"] not in output
