from __future__ import annotations

import hashlib
import json
import os
import secrets
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
CREATE INDEX IF NOT EXISTS message_source_window ON messages(source_id, date);
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
CREATE TABLE IF NOT EXISTS message_events (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
    message_id INTEGER NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS event_message ON message_events(source_id,message_id,id);
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY, granted TEXT NOT NULL, selected TEXT NOT NULL,
    language TEXT NOT NULL, joined_at TEXT NOT NULL, window_end TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY, granted TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_cache (
    key TEXT PRIMARY KEY, payload BLOB NOT NULL, created_at TEXT NOT NULL
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
        if version not in (0, 1, 2, 3):
            raise ValueError("Unsupported database schema")
        # Fail early on a wrong encryption key, before collecting or sending anything.
        check = self.get("key_check")
        if check is None:
            self.set("key_check", self.encrypt("ufifeed").decode())
        elif self.decrypt(check.encode()) != "ufifeed":
            raise ValueError("Incorrect data key")
        if version < 2:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'channel'"
                )
                self.conn.execute(
                    "ALTER TABLE sources ADD COLUMN peer_type TEXT NOT NULL DEFAULT 'channel'"
                )
                for column in ("deleted_at TEXT", "reaction_at TEXT", "details BLOB"):
                    self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column}")
                for row in self.conn.execute("SELECT * FROM messages").fetchall():
                    self._event(
                        row["source_id"],
                        row["id"],
                        "baseline",
                        {
                            "text": self.decrypt(row["text"]),
                            "date": row["date"],
                            "edited_at": row["edited_at"],
                            "reply_to": row["reply_to"],
                            "topic_id": row["topic_id"],
                            "deleted": bool(row["deleted"]),
                            "historical_versions_unavailable": True,
                        },
                    )
                self._set("schema_version", 2)

        if version < 3:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("ALTER TABLE runs ADD COLUMN recipient_id INTEGER")
                self._set("schema_version", 3)

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

    def subscriber(self, user_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM subscribers WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def subscribers(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM subscribers ORDER BY id")]

    def available_sources(self, recipient_id: int | None) -> list[dict]:
        subscriber = self.subscriber(recipient_id) if recipient_id is not None else None
        if recipient_id is None:
            return self.sources()
        granted = set(json.loads(subscriber["granted"])) if subscriber else set()
        return [source for source in self.sources() if source["id"] in granted]

    def selected_sources(self, recipient_id: int | None) -> set[int]:
        available = {source["id"] for source in self.available_sources(recipient_id)}
        if recipient_id is None:
            selected = self.get("owner_selected_sources")
        else:
            subscriber = self.subscriber(recipient_id)
            selected = json.loads(subscriber["selected"]) if subscriber else []
        return available if selected is None else available.intersection(selected)

    def select_sources(self, recipient_id: int | None, source_ids: set[int] | None) -> None:
        available = {source["id"] for source in self.available_sources(recipient_id)}
        if source_ids is None and recipient_id is not None:
            source_ids = available
        if source_ids is not None and not source_ids <= available:
            raise ValueError("Source is unavailable")
        if recipient_id is not None and not self.subscriber(recipient_id):
            raise ValueError("Subscriber is unavailable")
        selected = sorted(source_ids) if source_ids is not None else None
        with self.conn:
            if recipient_id is None:
                if self.get("owner_selected_sources") == selected:
                    return
                self._set("owner_selected_sources", selected)
            else:
                self.conn.execute(
                    "UPDATE subscribers SET selected=? WHERE id=?",
                    (json.dumps(selected), recipient_id),
                )
            self._expire_recipient(recipient_id)

    def enable_subscriber_source(self, user_id: int, source_id: int) -> None:
        subscriber = self.subscriber(user_id)
        if not subscriber:
            raise ValueError("Subscriber is unavailable")
        granted = set(json.loads(subscriber["granted"])) | {source_id}
        selected = set(json.loads(subscriber["selected"])) | {source_id}
        with self.conn:
            self.conn.execute(
                "UPDATE subscribers SET granted=?,selected=? WHERE id=?",
                (json.dumps(sorted(granted)), json.dumps(sorted(selected)), user_id),
            )

    def _expire_recipient(self, recipient_id: int | None) -> None:
        self.conn.execute(
            "UPDATE outbox SET payload=?,status='expired',error=NULL "
            "WHERE status IN ('pending','failed','unknown','sending') AND run_id IN "
            "(SELECT id FROM runs WHERE recipient_id IS ?)",
            (b"", recipient_id),
        )

    def create_invite(self, source_ids: set[int]) -> str:
        if not source_ids or not source_ids <= {s["id"] for s in self.sources()}:
            raise ValueError("Choose active sources for the invite")
        code = secrets.token_urlsafe(24)
        with self.conn:
            self.conn.execute("DELETE FROM invites WHERE expires_at<=?", (stamp(utcnow()),))
            self.conn.execute(
                "INSERT INTO invites VALUES (?,?,?)",
                (
                    hashlib.sha256(code.encode()).hexdigest(),
                    json.dumps(sorted(source_ids)),
                    stamp(utcnow() + timedelta(hours=24)),
                ),
            )
        return code

    def join(self, user_id: int, code: str) -> bool:
        with self.conn:
            invite = self.conn.execute(
                "DELETE FROM invites WHERE code_hash=? AND expires_at>? RETURNING granted",
                (hashlib.sha256(code.encode()).hexdigest(), stamp(utcnow())),
            ).fetchone()
            if not invite:
                return False
            now = stamp(utcnow())
            self._expire_recipient(user_id)
            self.conn.execute(
                "INSERT OR REPLACE INTO subscribers VALUES (?,?,?,?,?,?)",
                (
                    user_id,
                    invite["granted"],
                    invite["granted"],
                    self.get("language", "en"),
                    now,
                    now,
                ),
            )
            return True

    def revoke(self, user_id: int) -> bool:
        with self.conn:
            self._expire_recipient(user_id)
            return bool(
                self.conn.execute("DELETE FROM subscribers WHERE id=?", (user_id,)).rowcount
            )

    def subscriber_language(self, user_id: int, language: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE subscribers SET language=? WHERE id=?", (language, user_id))
            self._expire_recipient(user_id)

    def sync_folder(self, sources: list[dict], since: datetime) -> None:
        """Apply membership only after every peer has been resolved successfully."""
        active = {s["id"] for s in self.sources()}
        with self.conn:
            if active - {source["id"] for source in sources}:
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
                self.conn.execute(
                    "UPDATE sources SET kind=?,peer_type=? WHERE id=?",
                    (source.get("kind", "channel"), source.get("peer_type", "channel"), source_id),
                )
                if source_id not in active:
                    self.conn.execute(
                        "UPDATE sources SET cursor=0,initialized=0,since=?,"
                        "last_sync=NULL,error=NULL,ai_allowed=? "
                        "WHERE id=?",
                        (stamp(since), int(self.get("ai_folder", False)), source_id),
                    )
            self.conn.execute("UPDATE sources SET ai_allowed=0 WHERE enabled=0")
            self._set("folder_synced_at", stamp(utcnow()))
            self._set("folder_error", None)

    def change_folder(self, folder_id: int | None) -> None:
        with self.conn:
            self._set("folder_id", folder_id)
            self._set("ai_folder", False)
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
                    for key in (
                        "folder_id",
                        "schedule",
                        "topics",
                        "exclude",
                        "mode",
                        "language",
                        "owner_selected_sources",
                        "content_epoch",
                    )
                },
                "sources": [(s["id"], s["ai_allowed"], s["kind"]) for s in self.sources()],
            },
            sort_keys=True,
        )

    def _event(self, source_id: int, message_id: int, kind: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO message_events(source_id,message_id,observed_at,kind,payload) "
            "VALUES (?,?,?,?,?)",
            (
                source_id,
                message_id,
                stamp(utcnow()),
                kind,
                self.encrypt(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ),
        )

    def save_message(self, source_id: int, item: dict) -> None:
        previous = self.conn.execute(
            "SELECT * FROM messages WHERE source_id=? AND id=?", (source_id, item["id"])
        ).fetchone()
        details = {
            key: item.get(key)
            for key in (
                "sender_id",
                "media_kind",
                "media_id",
                "entities",
                "grouped_id",
                "reactions",
                "reaction_count",
                "paid_reactions",
            )
        }
        old_details = (
            json.loads(self.decrypt(previous["details"]))
            if previous and previous["details"]
            else {}
        )
        if item.get("reactions") is None:
            for key in ("reactions", "reaction_count", "paid_reactions"):
                details[key] = old_details.get(key)
        edited = stamp(item["edited_at"]) if item.get("edited_at") else None
        # A delayed new-message event must not overwrite a later edit.
        if previous and previous["edited_at"] and (edited or "") < previous["edited_at"]:
            return
        content = {
            "text": item["text"],
            "reply_to": item.get("reply_to"),
            "topic_id": item.get("topic_id"),
            "edited_at": edited,
            **{
                k: details[k]
                for k in ("sender_id", "media_kind", "media_id", "entities", "grouped_id")
            },
        }
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        changed = previous is None or previous["content_hash"] != digest
        reactions_changed = any(
            details.get(k) != old_details.get(k)
            for k in ("reactions", "reaction_count", "paid_reactions")
        )
        if (
            previous
            and not changed
            and not reactions_changed
            and previous["date"] == stamp(item["date"])
        ):
            return
        reaction_at = (
            stamp(utcnow())
            if reactions_changed
            else (previous["reaction_at"] if previous else None)
        )
        self.conn.execute(
            """INSERT INTO messages(source_id,id,date,edited_at,text,content_hash,
            reply_to,topic_id,details,reaction_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,id) DO UPDATE SET
            date=excluded.date,edited_at=excluded.edited_at,text=excluded.text,
            content_hash=excluded.content_hash,
            reply_to=excluded.reply_to,topic_id=excluded.topic_id,
            details=excluded.details,reaction_at=excluded.reaction_at""",
            (
                source_id,
                item["id"],
                stamp(item["date"]),
                stamp(item["edited_at"]) if item.get("edited_at") else None,
                self.encrypt(item["text"]),
                digest,
                item.get("reply_to"),
                item.get("topic_id"),
                self.encrypt(json.dumps(details, ensure_ascii=False)),
                reaction_at,
            ),
        )
        if changed or reactions_changed:
            kind = "created" if previous is None else ("edited" if changed else "reactions")
            if (
                previous
                and previous["details"] is None
                and self.decrypt(previous["text"]) == item["text"]
            ):
                kind = "observed"
            elif (
                previous
                and previous["deleted"]
                and not self.decrypt(previous["text"])
                and item["text"]
            ):
                kind = "late_content"
            self._event(
                source_id,
                item["id"],
                kind,
                {
                    **content,
                    **details,
                    "date": stamp(item["date"]),
                    "edited_at": edited,
                    "deleted": bool(previous and previous["deleted"]),
                },
            )

    def upsert_live(self, source_id: int, item: dict) -> None:
        with self.conn:
            self.save_message(source_id, item)
        # Live updates MUST NOT advance the history cursor.

    def refresh_page(self, source_id: int, items: list[dict]) -> None:
        """Persist a history overlap with one durable commit, without moving its cursor."""
        with self.conn:
            for item in items:
                self.save_message(source_id, item)

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
            for msg_id in ids:
                row = self.conn.execute(
                    "SELECT * FROM messages WHERE source_id=? AND id=?", (source_id, msg_id)
                ).fetchone()
                if row and row["deleted"]:
                    continue
                if row is None:
                    self.save_message(source_id, {"id": msg_id, "date": utcnow(), "text": ""})
                self.conn.execute(
                    "UPDATE messages SET deleted=1,deleted_at=? WHERE source_id=? AND id=?",
                    (stamp(utcnow()), source_id, msg_id),
                )
                self._event(
                    source_id,
                    msg_id,
                    "deleted",
                    {
                        "text": self.decrypt(row["text"]) if row else "",
                        "content_observed": row is not None,
                    },
                )

    def redact(self, source_id: int, msg_id: int) -> None:
        """Forget protected/disappearing content and derived output."""
        if not self.conn.execute(
            "SELECT 1 FROM messages WHERE source_id=? AND id=?", (source_id, msg_id)
        ).fetchone():
            return
        with self.conn:
            self.conn.execute(
                "DELETE FROM message_events WHERE source_id=? AND message_id=?", (source_id, msg_id)
            )
            self.conn.execute(
                "DELETE FROM messages WHERE source_id=? AND id=?", (source_id, msg_id)
            )
            self.conn.execute("DELETE FROM analysis_cache")
            self._set("content_epoch", self.get("content_epoch", 0) + 1)
            self._invalidate_pending()

    def verification_ids(self, source_id: int) -> tuple[list[int], int]:
        recent = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM messages WHERE source_id=? AND deleted=0 "
                "ORDER BY id DESC LIMIT 200",
                (source_id,),
            )
        ]
        cursor = self.get(f"verify:{source_id}", 0)
        older = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM messages WHERE source_id=? AND deleted=0 AND id>? "
                "ORDER BY id LIMIT 100",
                (source_id, cursor),
            )
        ]
        return sorted(set(recent + older)), older[-1] if older else 0

    def has_recent_snapshot(self, now: datetime, max_age_seconds: int) -> bool:
        folder_synced_at = self.get("folder_synced_at")
        if not folder_synced_at or self.get("folder_error") or self.get("reader_retry_at"):
            return False
        cutoff = now - timedelta(seconds=max_age_seconds)
        return parse(folder_synced_at) >= cutoff and all(
            source["initialized"]
            and not source["error"]
            and source["last_sync"]
            and parse(source["last_sync"]) >= cutoff
            for source in self.sources()
        )

    def channel_rates(self, source_ids: set[int], end: datetime) -> dict[int, float | None]:
        """Observed posts per day, including quiet days, over up to two weeks."""
        cutoff = end - timedelta(days=14)
        rows = self.conn.execute(
            """SELECT s.id,s.since,COUNT(m.id) AS posts
            FROM sources s LEFT JOIN messages m ON m.source_id=s.id
                AND m.date>=MAX(s.since,?) AND m.date<?
            WHERE s.enabled=1 AND s.kind='channel' GROUP BY s.id""",
            (stamp(cutoff), stamp(end)),
        )
        rates = {}
        for row in rows:
            if row["id"] in source_ids:
                days = (end - max(cutoff, parse(row["since"]))).total_seconds() / 86400
                rates[row["id"]] = round(row["posts"] / days, 1) if days >= 1 else None
        return rates

    def candidates(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.conn.execute(
            """SELECT m.*,s.title,s.username,s.ai_allowed,s.kind FROM messages m
            JOIN sources s ON s.id=m.source_id WHERE s.enabled=1 AND (
            (m.date>=? AND m.date<?) OR (m.deleted_at>=? AND m.deleted_at<?)
            OR (m.edited_at>=? AND m.edited_at<?)
            OR (s.kind='group' AND m.reaction_at>=? AND m.reaction_at<?))
            ORDER BY m.date,m.source_id,m.id""",
            (stamp(start), stamp(end)) * 4,
        )
        return [
            {
                **dict(row),
                "text": self.decrypt(row["text"]),
                **(json.loads(self.decrypt(row["details"])) if row["details"] else {}),
            }
            for row in rows
        ]

    def cache_get(self, key: str) -> list[dict] | None:
        row = self.conn.execute("SELECT payload FROM analysis_cache WHERE key=?", (key,)).fetchone()
        return json.loads(self.decrypt(row[0])) if row else None

    def with_reply_context(self, rows: list[dict]) -> list[dict]:
        """Include archived parents, without importing unrelated chats or counting them as new."""
        result = list(rows)
        known = {(r["source_id"], r["id"]) for r in result}
        frontier = rows
        for _ in range(3):
            added = []
            for child in frontier:
                key = (child["source_id"], child.get("reply_to"))
                if not key[1] or key in known:
                    continue
                row = self.conn.execute(
                    "SELECT m.*,s.title,s.username,s.ai_allowed,s.kind FROM messages m "
                    "JOIN sources s ON s.id=m.source_id WHERE s.enabled=1 AND s.ai_allowed=1 "
                    "AND m.source_id=? AND m.id=?",
                    key,
                ).fetchone()
                if row:
                    added.append(
                        {
                            **dict(row),
                            "text": self.decrypt(row["text"]),
                            **(json.loads(self.decrypt(row["details"])) if row["details"] else {}),
                            "context_only": True,
                        }
                    )
                    known.add(key)
            result.extend(added)
            frontier = added
        return result

    def cache_put(self, key: str, result: list[dict]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO analysis_cache VALUES (?,?,?)",
                (key, self.encrypt(json.dumps(result, ensure_ascii=False)), stamp(utcnow())),
            )

    def journal_stats(self) -> dict:
        return {
            "messages": self.conn.execute("SELECT count(*) FROM messages").fetchone()[0],
            "deleted": self.conn.execute(
                "SELECT count(*) FROM messages WHERE deleted=1"
            ).fetchone()[0],
            "events": self.conn.execute("SELECT count(*) FROM message_events").fetchone()[0],
        }

    def message_history(self, source_id: int, message_id: int) -> list[dict]:
        return [
            {
                "observed_at": row["observed_at"],
                "kind": row["kind"],
                "payload": json.loads(self.decrypt(row["payload"])),
            }
            for row in self.conn.execute(
                "SELECT * FROM message_events WHERE source_id=? AND message_id=? ORDER BY id",
                (source_id, message_id),
            )
        ]

    def enqueue(
        self,
        slot: str,
        start: datetime,
        end: datetime,
        mode: str,
        parts: list[str],
        *,
        advance: bool,
        recipient_id: int | None = None,
    ) -> int | None:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO runs(slot,start,end,created_at,mode,recipient_id) "
                "VALUES (?,?,?,?,?,?)",
                (slot, stamp(start), stamp(end), stamp(utcnow()), mode, recipient_id),
            )
            if not cursor.rowcount:
                return None
            run_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO outbox(run_id,part,payload,next_attempt) VALUES (?,?,?,?)",
                [(run_id, i, self.encrypt(part), stamp(utcnow())) for i, part in enumerate(parts)],
            )
            if advance and recipient_id is None:
                self._set("window_end", stamp(end))
            elif advance:
                self.conn.execute(
                    "UPDATE subscribers SET window_end=? WHERE id=?", (stamp(end), recipient_id)
                )
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
                """SELECT o.*,r.recipient_id FROM outbox o JOIN runs r ON r.id=o.run_id
                WHERE o.status='pending' AND o.next_attempt<=?
                AND (r.recipient_id IS NULL OR EXISTS
                    (SELECT 1 FROM subscribers s WHERE s.id=r.recipient_id))
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
                "UPDATE outbox SET status='sent',message_id=?,error=NULL "
                "WHERE id=? AND status='sending'",
                (message_id, item_id),
            )

    def delivery_error(self, item_id: int, status: str, error: str, delay: int = 0) -> None:
        assert status in {"pending", "failed", "unknown"}
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status=?,error=?,next_attempt=? WHERE id=? AND status='sending'",
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
        cutoff = stamp((now or utcnow()) - timedelta(days=days or 30))
        with self.conn:
            if days:
                self.conn.execute(
                    "DELETE FROM message_events WHERE (source_id,message_id) IN "
                    "(SELECT source_id,id FROM messages WHERE date<? AND "
                    "COALESCE(deleted_at,date)<?)",
                    (cutoff, cutoff),
                )
                self.conn.execute(
                    "DELETE FROM messages WHERE date<? AND COALESCE(deleted_at,date)<?",
                    (cutoff, cutoff),
                )
            self.conn.execute("DELETE FROM analysis_cache WHERE created_at<?", (cutoff,))
            # Keep minimal run keys; remove private payloads and disable retries after retention.
            self.conn.execute(
                "UPDATE outbox SET payload=?,status='expired',error=NULL WHERE run_id IN "
                "(SELECT id FROM runs WHERE created_at<?)",
                (b"", cutoff),
            )

    def purge(self, source_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM message_events WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM messages WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM analysis_cache")
            self.conn.execute("UPDATE sources SET ai_allowed=0 WHERE id=?", (source_id,))
            # Digests can combine sources; remove all retained payloads conservatively.
            self.conn.execute("UPDATE outbox SET payload=?,status='expired',error=NULL", (b"",))

    def last_window_end(self) -> datetime:
        return parse(self.get("window_end"))
