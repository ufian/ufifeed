from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from ufifeed.config import Config, Credentials
from ufifeed.store import Store
from ufifeed.timeutil import utcnow


@pytest.fixture
def config(tmp_path):
    (tmp_path / "secrets").mkdir()
    return Config.model_validate(
        {
            "owner": {"user_id": 123},
            "storage": {
                "database_path": tmp_path / "data/db.sqlite3",
                "key_path": tmp_path / "secrets/key",
            },
            "telegram": {"session_path": tmp_path / "secrets/reader.session"},
        }
    )


@pytest.fixture
def credentials():
    return Credentials(api_id=123, api_hash="hash", bot_token="123:TEST")


@pytest.fixture
def store(config):
    config.storage.key_path.write_bytes(Fernet.generate_key())
    db = Store.open(config)
    db.bootstrap(config)
    db.sync_folder(
        [{"id": -1000000000123, "title": "Source", "username": "source"}],
        utcnow() - timedelta(hours=24),
    )
    yield db
    db.close()


@pytest.fixture
def item():
    return {"id": 10, "date": utcnow() - timedelta(minutes=10), "text": "Important release"}
