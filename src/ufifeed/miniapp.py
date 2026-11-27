from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime
from importlib.resources import files
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from aiohttp import web
from pydantic import Field, ValidationError, field_validator, model_validator

from ufifeed.config import Config, Credentials, Schedule, StrictModel
from ufifeed.digest import DigestError, message_link
from ufifeed.store import Store
from ufifeed.timeutil import calendar_window, stamp, utcnow

log = logging.getLogger(__name__)
RECIPIENT = web.RequestKey("recipient_id", int | None)


def authenticate(raw: str, token: str, owner_id: int | None, *, now: float | None = None) -> int:
    """Validate raw Telegram initData, freshness and the numeric identity."""
    try:
        if not raw or len(raw) > 16000:
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError
        signature = data.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        age = (time.time() if now is None else now) - int(data["auth_date"])
        user = json.loads(data["user"])
        if not -30 <= age <= 3600 or type(user["id"]) is not int or user["id"] <= 0:
            raise ValueError
        if owner_id is not None and user["id"] != owner_id:
            raise ValueError
        return user["id"]
    except (ValueError, KeyError, TypeError, OverflowError):
        raise web.HTTPUnauthorized(
            text="Reopen the Mini App from the paired Telegram account."
        ) from None


class Settings(StrictModel):
    revision: str = Field(max_length=100000)
    language: Literal["en", "ru"]
    mode: Literal["ai", "extractive"]
    topics: list[str] = Field(max_length=50)
    exclude: list[str] = Field(max_length=50)
    schedule: Schedule

    @field_validator("topics", "exclude")
    @classmethod
    def keywords(cls, words: list[str]) -> list[str]:
        if any(not word.strip() or len(word) > 100 for word in words):
            raise ValueError("Keywords must contain 1–100 characters")
        return list(dict.fromkeys(word.strip() for word in words))


class SourcePreferences(StrictModel):
    source_ids: list[int] | None
    revision: str = Field(max_length=64)


class Action(StrictModel):
    action: Literal["folder", "ai_source", "ai_folder"]
    revision: str = Field(max_length=100000)
    id: int = 0
    enabled: bool = False


class DigestQuery(StrictModel):
    period: Literal["recent", "day", "week"] = "recent"
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_period(self) -> DigestQuery:
        if (self.period == "recent") != (self.date is None):
            raise ValueError("A day or week requires a date")
        return self


class JournalQuery(StrictModel):
    source_id: int | None = None
    deleted_only: bool = False
    before: int = Field(default=0, ge=0)


class HistoryQuery(StrictModel):
    source_id: int
    message_id: int = Field(gt=0)
    before: int = Field(default=0, ge=0)


class MiniApp:
    def __init__(self, config: Config, credentials: Credentials, store: Store, controls):
        self.config, self.credentials, self.store, self.controls = (
            config,
            credentials,
            store,
            controls,
        )
        self.digest_task: asyncio.Task | None = None
        self.app = web.Application(middlewares=[self.boundary], client_max_size=16384)
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/preferences", self.preferences_page),
                web.post("/api/preferences/state", self.preferences_state),
                web.post("/api/preferences/save", self.preferences_save),
                web.get("/assets/{name}", self.asset),
                web.get("/health", self.health),
                web.post("/api/state", self.state),
                web.post("/api/folders", self.folders),
                web.post("/api/settings", self.settings),
                web.post("/api/action", self.action),
                web.post("/api/digest", self.digest),
                web.post("/api/journal", self.journal),
                web.post("/api/history", self.history),
            ]
        )
        self.app.on_cleanup.append(self.cleanup)

    @web.middleware
    async def boundary(self, request: web.Request, handler):
        try:
            if request.path.startswith("/api/"):
                authorization = request.headers.get("Authorization", "")
                if not authorization.startswith("tma "):
                    raise web.HTTPUnauthorized(text="Open this app from Telegram.")
                preferences = request.path in {"/api/preferences/state", "/api/preferences/save"}
                user_id = authenticate(
                    authorization[4:],
                    self.credentials.bot_token.get_secret_value(),
                    None if preferences else self.config.owner.user_id,
                )
                recipient = None if user_id == self.config.owner.user_id else user_id
                if recipient is not None and not self.store.subscriber(recipient):
                    raise web.HTTPUnauthorized(text="You need an invitation to subscribe.")
                request[RECIPIENT] = recipient
                origin = request.headers.get("Origin")
                target = urlsplit(self.config.miniapp.public_url)
                expected = f"{target.scheme}://{target.netloc}"
                if origin and origin != expected:
                    raise web.HTTPForbidden(text="Unexpected request origin.")
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response(
                {"error": exc.text if exc.status < 500 else "Request failed."}, status=exc.status
            )
        except (ValidationError, ValueError, KeyError, TypeError):
            response = web.json_response({"error": "Check the submitted values."}, status=400)
        except DigestError as exc:
            response = web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            log.warning("Mini App request failed: %s", type(exc).__name__)
            response = web.json_response({"error": "Request failed. Please try again."}, status=500)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; "
                "script-src 'self' https://telegram.org; "
                "style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'self'; "
                "form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org",
            }
        )
        return response

    async def index(self, request):
        return web.Response(
            text=files("ufifeed").joinpath("web/index.html").read_text(), content_type="text/html"
        )

    async def preferences_page(self, request):
        return web.Response(
            text=files("ufifeed").joinpath("web/preferences.html").read_text(),
            content_type="text/html",
        )

    def preference_data(self, recipient_id: int | None) -> dict:
        selected = self.store.selected_sources(recipient_id)
        sources = [
            {"id": str(s["id"]), "title": s["title"], "selected": s["id"] in selected}
            for s in self.store.available_sources(recipient_id)
        ]
        follow_all = recipient_id is None and self.store.get("owner_selected_sources") is None
        data = {"sources": sources, "owner": recipient_id is None, "follow_all": follow_all}
        subscriber = self.store.subscriber(recipient_id) if recipient_id is not None else None
        revision = hashlib.sha256(
            json.dumps([data, subscriber and subscriber["joined_at"]]).encode()
        ).hexdigest()
        return {**data, "revision": revision}

    async def preferences_state(self, request):
        return web.json_response(self.preference_data(request[RECIPIENT]))

    async def preferences_save(self, request):
        data = SourcePreferences.model_validate(await request.json())
        recipient_id = request[RECIPIENT]
        if data.revision != self.preference_data(recipient_id)["revision"]:
            raise web.HTTPConflict(text="Source settings changed. Reopen settings and try again.")
        if recipient_id is not None and data.source_ids is None:
            raise ValueError("Select granted sources explicitly")
        self.store.select_sources(
            recipient_id, set(data.source_ids) if data.source_ids is not None else None
        )
        return web.json_response(self.preference_data(recipient_id))

    async def asset(self, request):
        name = request.match_info["name"]
        if name not in {"app.js", "app.css", "preferences.js"}:
            raise web.HTTPNotFound()
        return web.Response(
            text=files("ufifeed").joinpath(f"web/{name}").read_text(),
            content_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    async def health(self, request):
        return web.json_response({"ok": True})

    def revision(self) -> str:
        return hashlib.sha256(self.store.input_signature().encode()).hexdigest()

    def check_revision(self, revision: str) -> None:
        if revision != self.revision():
            raise web.HTTPConflict(text="Settings changed. Refresh before saving again.")

    def state_data(self) -> dict:
        sources = self.store.sources(active_only=False)
        counts = {
            r["source_id"]: r["n"]
            for r in self.store.conn.execute(
                "SELECT source_id,count(*) AS n FROM messages GROUP BY source_id"
            )
        }
        runs = []
        for row in self.store.conn.execute(
            "SELECT * FROM runs WHERE recipient_id IS NULL ORDER BY id DESC LIMIT 10"
        ):
            parts = [
                {
                    "status": p["status"],
                    "text": self.store.decrypt(p["payload"]) if p["payload"] else "",
                }
                for p in self.store.conn.execute(
                    "SELECT status,payload FROM outbox WHERE run_id=? ORDER BY part", (row["id"],)
                )
            ]
            if parts:
                runs.append({"id": row["id"], "created_at": row["created_at"], "parts": parts})
        return {
            "revision": self.revision(),
            "folder": self.store.get("folder_title"),
            "folder_id": self.store.get("folder_id"),
            "ai_folder": self.store.get("ai_folder", False),
            "ai_available": self.config.ai.enabled,
            "language": self.store.get("language", "en"),
            "mode": self.store.get("mode"),
            "topics": self.store.get("topics", []),
            "exclude": self.store.get("exclude", []),
            "schedule": self.store.get("schedule"),
            "retention_days": self.config.storage.retention_days,
            "journal": self.store.journal_stats(),
            "sources": [
                {
                    k: source[k]
                    for k in ("id", "title", "kind", "enabled", "ai_allowed", "last_sync", "error")
                }
                | {"messages": counts.get(source["id"], 0)}
                for source in sources
            ],
            "digests": runs,
            "busy": self.controls.digests.lock.locked()
            or bool(self.digest_task and not self.digest_task.done()),
            "ai_progress": self.store.get("ai_progress"),
            "reader_retry_at": self.store.get("reader_retry_at"),
            "last_ai_input": self.store.get("last_ai_input"),
            "error": self.store.get("digest_error") or self.store.get("folder_error"),
            "manual_job": self.store.get("manual_job"),
            "preparation_started_at": self.controls.digests.preparation_started_at,
            "digest_today": utcnow()
            .astimezone(ZoneInfo(self.store.get("schedule")["timezone"]))
            .date()
            .isoformat(),
        }

    async def state(self, request):
        return web.json_response(self.state_data())

    async def folders(self, request):
        return web.json_response(
            {
                "folders": [
                    {"id": f["id"], "title": f["title"]}
                    for f in await self.controls.reader.folders()
                ]
            }
        )

    async def settings(self, request):
        data = Settings.model_validate(await request.json())
        self.check_revision(data.revision)
        if data.mode == "ai" and not self.config.ai.enabled:
            raise web.HTTPConflict(text="AI is not configured on the server.")
        old = Schedule.model_validate(self.store.get("schedule"))
        with self.store.conn:
            for key in ("language", "mode", "topics", "exclude", "schedule"):
                self.store._set(key, data.model_dump()[key])
            # Match /schedule for time changes; pause/resume preserves the boundary.
            if old.times != data.schedule.times or old.timezone != data.schedule.timezone:
                self.store._set("window_end", stamp(utcnow()))
            self.store._invalidate_pending()
        return web.json_response(self.state_data())

    async def action(self, request):
        data = Action.model_validate(await request.json())
        self.check_revision(data.revision)
        if data.action == "folder":
            if data.id < 0:
                raise ValueError
            result = await self.controls.execute("/folder", str(data.id))
        elif data.action == "ai_source":
            result = await self.controls.execute(
                "/ai_source", f"{data.id} {'on' if data.enabled else 'off'}"
            )
        else:
            result = await self.controls.execute("/ai_folder", "on" if data.enabled else "off")
        return web.json_response({"message": result})

    async def _prepare_digest(self, window: tuple[datetime, datetime] | None):
        self.store.set("digest_error", None)
        self.store.set("manual_job", {"state": "running", "requested_at": stamp(utcnow())})
        try:
            if window is None:
                run_id = await self.controls.digests.manual()
            else:
                run_id = await self.controls.digests.manual(window)
            self.store.set("manual_job", {"state": "queued", "run_id": run_id})
            self.store.set("digest_error", None)
        except asyncio.CancelledError:
            self.store.set("manual_job", {"state": "interrupted"})
            raise
        except DigestError as exc:
            self.store.set("digest_error", str(exc))
            self.store.set("manual_job", {"state": "failed"})
        except Exception as exc:
            self.store.set("digest_error", type(exc).__name__)
            self.store.set("manual_job", {"state": "failed"})

    async def digest(self, request):
        query = DigestQuery.model_validate(await request.json() if request.can_read_body else {})
        window = None
        if query.period != "recent":
            try:
                window = calendar_window(
                    date.fromisoformat(query.date),
                    query.period,
                    self.store.get("schedule")["timezone"],
                    utcnow(),
                )
            except (ValueError, OverflowError):
                raise web.HTTPBadRequest(text="Choose a valid date in the past or today.") from None
        if self.controls.digests.lock.locked() or (
            self.digest_task and not self.digest_task.done()
        ):
            raise web.HTTPConflict(text="A digest is already being prepared.")
        self.digest_task = asyncio.create_task(self._prepare_digest(window), name="miniapp-digest")
        return web.json_response(
            {"message": "Preparing a preview for delivery in Telegram."}, status=202
        )

    async def journal(self, request):
        data = JournalQuery.model_validate(await request.json())
        clauses, args = [], []
        if data.source_id is not None:
            clauses.append("m.source_id=?")
            args.append(data.source_id)
        if data.deleted_only:
            clauses.append("m.deleted=1")
        if data.before:
            clauses.append("m.rowid<?")
            args.append(data.before)
        where = " AND ".join(clauses) or "1"
        rows = self.store.conn.execute(
            "SELECT m.rowid AS sequence,m.*,s.title FROM messages m "
            f"JOIN sources s ON s.id=m.source_id WHERE {where} ORDER BY m.rowid DESC LIMIT 51",
            args,
        ).fetchall()
        result = []
        for row in rows[:50]:
            details = json.loads(self.store.decrypt(row["details"])) if row["details"] else {}
            result.append(
                {
                    key: row[key]
                    for key in (
                        "sequence",
                        "source_id",
                        "id",
                        "title",
                        "date",
                        "edited_at",
                        "deleted",
                        "deleted_at",
                    )
                }
                | {
                    "text": self.store.decrypt(row["text"]),
                    "reactions": details.get("reaction_count") or 0,
                    "media": details.get("media_kind"),
                    "link": message_link(dict(row)),
                }
            )
        return web.json_response(
            {"messages": result, "next": result[-1]["sequence"] if len(rows) > 50 else None}
        )

    async def history(self, request):
        data = HistoryQuery.model_validate(await request.json())
        rows = self.store.conn.execute(
            "SELECT * FROM message_events WHERE source_id=? AND message_id=? "
            "AND (?=0 OR id<?) ORDER BY id DESC LIMIT 101",
            (data.source_id, data.message_id, data.before, data.before),
        ).fetchall()
        return web.json_response(
            {
                "events": [
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "observed_at": row["observed_at"],
                        "snapshot": json.loads(self.store.decrypt(row["payload"])),
                    }
                    for row in rows[:100]
                ],
                "next": rows[99]["id"] if len(rows) > 100 else None,
            }
        )

    async def cleanup(self, app):
        if self.digest_task and not self.digest_task.done():
            self.digest_task.cancel()
            await asyncio.gather(self.digest_task, return_exceptions=True)

    async def run(self):
        runner = web.AppRunner(self.app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", self.config.miniapp.port).start()
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
