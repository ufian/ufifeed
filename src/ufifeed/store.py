from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from ufifeed.config import Config
from ufifeed.timeutil import parse, stamp, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, ai_allowed INTEGER NOT NULL DEFAULT 0,
    cursor INTEGER NOT NULL DEFAULT 0, initialized INTEGER NOT NULL DEFAULT 0,
    since TEXT NOT NULL, last_sync TEXT, error TEXT,
    include_words TEXT NOT NULL DEFAULT '[]', exclude_words TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    source_id INTEGER NOT NULL REFERENCES sources(id), id INTEGER NOT NULL,
    date TEXT NOT NULL, edited_at TEXT, text BLOB NOT NULL, content_hash TEXT NOT NULL,
    reply_to INTEGER, topic_id INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source_id, id)
);
CREATE INDEX IF NOT EXISTS message_window ON messages(date, source_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, slot TEXT NOT NULL UNIQUE, start TEXT NOT NULL,
    end TEXT NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
    part INTEGER NOT NULL, payload BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    message_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT NOT NULL, error TEXT, UNIQUE(run_id, part)
);
CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0
);
"""


def create_key(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation prevents replacing the key for an existing database.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(Fernet.generate_key())


class Store:
    """Short synchronous transactions; used only on the application's event-loop thread."""

    def __init__(self, path: Path, key: bytes):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(key)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        os.chmod(path, 0o600)
        version = self.get("schema_version", 0)
        if version not in (0, 1):
            raise ValueError("Unsupported database schema")
        if version == 0:
            self.set("schema_version", 1)
        # Fail early on a wrong encryption key, before collecting or sending anything.
        check = self.get("key_check")
        if check is None:
            self.set("key_check", self.encrypt("ufifeed").decode())
        elif self.decrypt(check.encode()) != "ufifeed":
            raise ValueError("Incorrect data key")

    @classmethod
    def open(cls, config: Config) -> Store:
        return cls(config.storage.database_path, config.storage.key_path.read_bytes().strip())

    def close(self) -> None:
        self.conn.close()

    def encrypt(self, value: str) -> bytes:
        return self.cipher.encrypt(value.encode())

    def decrypt(self, value: bytes) -> str:
        return self.cipher.decrypt(value).decode()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.conn:
            self._set(key, value)

    def _set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def bootstrap(self, config: Config) -> None:
        if self.get("bootstrapped"):
            return
        with self.conn:
            self._set("schedule", config.schedule.model_dump())
            self._set("folder_id", config.telegram.folder_id)
            self._set(
                "window_end",
                stamp(
                    utcnow()
                    - timedelta(
                        hours=config.telegram.initial_history_hours,
                    )
                ),
            )
            self._set("topics", [])
            self._set("exclude", [])
            self._set("mode", "extractive")
            self._set("language", "en")
            self._set("started", False)
            self._set("bootstrapped", True)

    def sources(self, *, active_only: bool = True) -> list[dict]:
        where = "WHERE enabled=1" if active_only else ""
        return [
            dict(row) for row in self.conn.execute(f"SELECT * FROM sources {where} ORDER BY id")
        ]

    def sync_folder(self, sources: list[dict], since: datetime) -> None:
        """Apply membership only after every peer has been resolved successfully."""
        active = {s["id"] for s in self.sources()}
        with self.conn:
            if active != {source["id"] for source in sources}:
                self._invalidate_pending()
            self.conn.execute("UPDATE sources SET enabled=0")
            for source in sources:
                source_id = source["id"]
                self.conn.execute(
                    """INSERT INTO sources(id,title,username,since) VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                    username=excluded.username, enabled=1""",
                    (source_id, source["title"], source.get("username"), stamp(since)),
                )
                if source_id not in active:
                    self.conn.execute(
                        "UPDATE sources SET cursor=0,initialized=0,since=?,"
                        "last_sync=NULL,error=NULL,ai_allowed=0 "
                        "WHERE id=?",
                        (stamp(since), source_id),
                    )
            self.conn.execute("UPDATE sources SET ai_allowed=0 WHERE enabled=0")
            self._set("folder_synced_at", stamp(utcnow()))
            self._set("folder_error", None)

    def change_folder(self, folder_id: int | None) -> None:
        with self.conn:
            self._set("folder_id", folder_id)
            self._set("folder_synced_at", None)
            self._set("folder_error", "sync_pending" if folder_id else None)
            self.conn.execute("UPDATE sources SET enabled=0,ai_allowed=0")
            self._invalidate_pending()

    def _invalidate_pending(self) -> None:
        self.conn.execute(
            "UPDATE outbox SET payload=?,status='expired',error=NULL "
            "WHERE status IN ('pending','failed','unknown')",
            (b"",),
        )

    def invalidate_pending(self) -> None:
        with self.conn:
            self._invalidate_pending()

    def input_signature(self) -> str:
        return json.dumps(
            {
                "settings": {
                    key: self.get(key)
                    for key in ("folder_id", "schedule", "topics", "exclude", "mode", "language")
                },
                "sources": [(s["id"], s["ai_allowed"]) for s in self.sources()],
            },
            sort_keys=True,
        )

    def save_message(self, source_id: int, item: dict) -> None:
        self.conn.execute(
            """INSERT INTO messages(source_id,id,date,edited_at,text,content_hash,reply_to,topic_id)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id,id) DO UPDATE SET
            edited_at=excluded.edited_at,text=excluded.text,content_hash=excluded.content_hash,
            reply_to=excluded.reply_to,topic_id=excluded.topic_id,deleted=0""",
            (
                source_id,
                item["id"],
                stamp(item["date"]),
                stamp(item["edited_at"]) if item.get("edited_at") else None,
                self.encrypt(item["text"]),
                hashlib.sha256(item["text"].encode()).hexdigest(),
                item.get("reply_to"),
                item.get("topic_id"),
            ),
        )

    def upsert_live(self, source_id: int, item: dict) -> None:
        with self.conn:
            self.save_message(source_id, item)
        # Live updates MUST NOT advance the history cursor.

    def commit_page(self, source_id: int, items: list[dict], cursor: int) -> None:
        with self.conn:
            for item in items:
                self.save_message(source_id, item)
            self.conn.execute(
                "UPDATE sources SET cursor=MAX(cursor,?) WHERE id=?", (cursor, source_id)
            )

    def synced(self, source_id: int, at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET initialized=1,last_sync=?,error=NULL WHERE id=?",
                (stamp(at), source_id),
            )

    def source_error(self, source_id: int, error: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sources SET error=? WHERE id=?", (error, source_id))

    def delete_messages(self, source_id: int, ids: list[int]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE messages SET deleted=1,text=? WHERE source_id=? AND id=?",
                [(self.encrypt(""), source_id, msg_id) for msg_id in ids],
            )

    def candidates(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.conn.execute(
            """SELECT m.*,s.title,s.username,s.ai_allowed FROM messages m
            JOIN sources s ON s.id=m.source_id WHERE s.enabled=1 AND m.deleted=0
            AND m.date>=? AND m.date<? ORDER BY m.date DESC,m.source_id,m.id LIMIT 5000""",
            (stamp(start), stamp(end)),
        )
        return [{**dict(row), "text": self.decrypt(row["text"])} for row in rows]

    def enqueue(
        self,
        slot: str,
        start: datetime,
        end: datetime,
        mode: str,
        parts: list[str],
        *,
        advance: bool,
    ) -> int | None:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO runs(slot,start,end,created_at,mode) VALUES (?,?,?,?,?)",
                (slot, stamp(start), stamp(end), stamp(utcnow()), mode),
            )
            if not cursor.rowcount:
                return None
            run_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO outbox(run_id,part,payload,next_attempt) VALUES (?,?,?,?)",
                [(run_id, i, self.encrypt(part), stamp(utcnow())) for i, part in enumerate(parts)],
            )
            if advance:
                self._set("window_end", stamp(end))
            return run_id

    def recover_sending(self) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status='unknown',error='interrupted_send' "
                "WHERE status='sending'",
            )

    def claim(self) -> dict | None:
        with self.conn:
            row = self.conn.execute(
                """SELECT o.* FROM outbox o WHERE status='pending' AND next_attempt<=?
                AND NOT EXISTS (SELECT 1 FROM outbox prev WHERE prev.run_id=o.run_id
                    AND prev.part<o.part AND prev.status!='sent') ORDER BY o.id LIMIT 1""",
                (stamp(utcnow()),),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],)
            )
        return {**dict(row), "payload": self.decrypt(row["payload"])}

    def delivered(self, item_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status='sent',message_id=?,error=NULL WHERE id=?",
                (message_id, item_id),
            )

    def delivery_error(self, item_id: int, status: str, error: str, delay: int = 0) -> None:
        assert status in {"pending", "failed", "unknown"}
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status=?,error=?,next_attempt=? WHERE id=?",
                (status, error, stamp(utcnow() + timedelta(seconds=delay)), item_id),
            )

    def retry(self, item_id: int) -> bool:
        with self.conn:
            result = self.conn.execute(
                "UPDATE outbox SET status='pending',error=NULL,next_attempt=? "
                "WHERE id=? AND status IN ('unknown','failed')",
                (stamp(utcnow()), item_id),
            )
            return bool(result.rowcount)

    def delivery_status(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status"))

    def delivery_issues(self) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT id,status,error FROM outbox WHERE status IN ('unknown','failed') LIMIT 20",
            )
        ]

    def reserve_ai_call(self, limit: int) -> bool:
        day = utcnow().date().isoformat()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO ai_usage(day) VALUES (?)", (day,))
            calls = self.conn.execute("SELECT calls FROM ai_usage WHERE day=?", (day,)).fetchone()[
                0
            ]
            if calls >= limit:
                return False
            self.conn.execute("UPDATE ai_usage SET calls=calls+1 WHERE day=?", (day,))
            return True

    def cleanup(self, days: int, now: datetime | None = None) -> None:
        cutoff = stamp((now or utcnow()) - timedelta(days=days))
        with self.conn:
            self.conn.execute("DELETE FROM messages WHERE date<?", (cutoff,))
            # Keep minimal run keys; remove private payloads and disable retries after retention.
            self.conn.execute(
                "UPDATE outbox SET payload=?,status='expired',error=NULL WHERE run_id IN "
                "(SELECT id FROM runs WHERE created_at<?)",
                (b"", cutoff),
            )

    def purge(self, source_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM messages WHERE source_id=?", (source_id,))
            self.conn.execute("UPDATE sources SET ai_allowed=0 WHERE id=?", (source_id,))
            # Digests can combine sources; remove all retained payloads conservatively.
            self.conn.execute("UPDATE outbox SET payload=?,status='expired',error=NULL", (b"",))

    def last_window_end(self) -> datetime:
        return parse(self.get("window_end"))
