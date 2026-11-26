import json
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize("age,expected", [(0, 0), (120, 1), (-120, 1)])
def test_container_probe_checks_freshness_without_telegram_imports(tmp_path, age, expected):
    config = tmp_path / "config.toml"
    config.write_text(f"[storage]\ndatabase_path = {json.dumps(str(tmp_path / 'db.sqlite3'))}\n")
    (tmp_path / "heartbeat").write_text(str(time.time() - age))
    result = subprocess.run(
        [sys.executable, "-m", "ufifeed.healthcheck", "--config", str(config)],
        capture_output=True,
        timeout=2,
    )
    assert result.returncode == expected


def test_probe_does_not_load_heavy_application_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ufifeed.healthcheck; "
            "assert not {'aiogram', 'telethon', 'pydantic', 'cryptography'} & sys.modules.keys()",
        ],
        capture_output=True,
        timeout=2,
    )
    assert result.returncode == 0
