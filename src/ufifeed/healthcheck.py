"""A lightweight container probe that does not import Telegram or AI dependencies."""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from pathlib import Path


def check(config_path: Path) -> None:
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    database = Path(config.get("storage", {}).get("database_path", "data/ufifeed.sqlite3"))
    timestamp = float((database.parent / "heartbeat").read_text())
    if not 0 <= time.time() - timestamp < 60:
        raise RuntimeError("Stale heartbeat")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/ufifeed.toml"))
    args = parser.parse_args()
    try:
        check(args.config)
    except Exception as exc:
        print(f"Healthcheck failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
