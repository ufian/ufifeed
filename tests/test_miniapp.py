import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ufifeed.bot import Controls
from ufifeed.config import MiniApp as MiniAppConfig
from ufifeed.digest import Digests
from ufifeed.miniapp import MiniApp, authenticate
from ufifeed.timeutil import utcnow


def init_data(token="123:TEST", user=123, at=None, extra=None):
    fields = {
        "auth_date": str(int(time.time()) if at is None else at),
        "user": json.dumps({"id": user}),
    }
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", token.encode(), "sha256")
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def headers(**kwargs):
    return {"Authorization": "tma " + init_data(**kwargs)}


@pytest.fixture
def miniapp(config, credentials, store):
    config.miniapp = MiniAppConfig(enabled=True, public_url="https://ufifeed.example:8443/")
    reader = SimpleNamespace(folders=AsyncMock(return_value=[]), wake=asyncio.Event())
    digests = Digests(config, credentials, store, reader)
    controls = Controls(config, store, reader, digests)
    return MiniApp(config, credentials, store, controls)


def test_signature_owner_freshness_and_duplicate_checks():
    authenticate(init_data(at=10000), "123:TEST", 123, now=10010)
    authenticate(init_data(at=10000, extra={"signature": "opaque"}), "123:TEST", 123, now=10010)
    for raw in (
        init_data(user=456, at=10000),
        init_data(user=True, at=10000),
        init_data(at=10000) + "&auth_date=10000",
        init_data(at=1),
        init_data(at=20000),
        init_data(token="wrong", at=10000),
        "hash=bad",
    ):
        with pytest.raises(web.HTTPUnauthorized):
            authenticate(raw, "123:TEST", 123, now=10010)


async def test_public_shell_and_owner_only_apis(miniapp, store, item):
    store.upsert_live(-1000000000123, item)
    async with TestClient(TestServer(miniapp.app)) as client:
        public = await client.get("/")
        assert public.status == 200 and "Your signal" in await public.text()
        assert item["text"] not in await public.text()
        assert public.headers["Cache-Control"] == "no-store"
        assert "script-src 'self'" in public.headers["Content-Security-Policy"]
        for route in ("state", "journal", "history", "settings", "action", "digest", "folders"):
            response = await client.post("/api/" + route, json={})
            assert response.status == 401
        response = await client.post("/api/state", headers=headers(user=456), json={})
        assert response.status == 401
        response = await client.post(
            "/api/state", headers=headers() | {"Origin": "https://evil.example"}, json={}
        )
        assert response.status == 403
        response = await client.post("/api/state", headers=headers(), json={})
        data = await response.json()
        assert response.status == 200 and data["journal"]["messages"] == 1
        assert "123:TEST" not in json.dumps(data) and "payload" not in json.dumps(data)
        assert (await client.get("/assets/app.js")).status == 200
        assert (await client.get("/assets/credentials.json")).status == 404


async def test_settings_are_validated_atomic_and_detect_concurrent_edits(miniapp, store):
    data = {
        "revision": miniapp.revision(),
        "language": "ru",
        "mode": "extractive",
        "topics": [],
        "exclude": [],
        "schedule": {"enabled": False, "times": ["09:00"], "timezone": "America/Los_Angeles"},
    }
    async with TestClient(TestServer(miniapp.app)) as client:
        response = await client.post("/api/settings", headers=headers(), json=data)
        assert response.status == 200 and store.get("language") == "ru"
        response = await client.post("/api/settings", headers=headers(), json=data)
        assert response.status == 409
        data.update(revision=miniapp.revision(), language="invalid")
        assert (await client.post("/api/settings", headers=headers(), json=data)).status == 400
        assert store.get("language") == "ru"
        data.update(
            language="en", schedule={"enabled": True, "times": ["25:00"], "timezone": "Seattle"}
        )
        assert (await client.post("/api/settings", headers=headers(), json=data)).status == 400
        assert store.get("language") == "ru"


async def test_pause_retains_window_while_new_times_start_now(miniapp, store):
    data = {
        "revision": miniapp.revision(),
        "language": "en",
        "mode": "extractive",
        "topics": [],
        "exclude": [],
        "schedule": store.get("schedule"),
    }
    start = store.last_window_end()
    data["schedule"]["enabled"] = True
    async with TestClient(TestServer(miniapp.app)) as client:
        assert (await client.post("/api/settings", headers=headers(), json=data)).status == 200
        assert store.last_window_end() == start
        data["revision"] = miniapp.revision()
        data["schedule"]["times"] = ["10:00"]
        assert (await client.post("/api/settings", headers=headers(), json=data)).status == 200
        assert store.last_window_end() > start


async def test_deleted_journal_versions_and_pagination(miniapp, store, item):
    source = -1000000000123
    store.commit_page(source, [{**item, "id": i} for i in range(1, 61)], 60)
    store.upsert_live(source, {**item, "id": 1, "text": "Updated", "edited_at": utcnow()})
    store.delete_messages(source, [1])
    async with TestClient(TestServer(miniapp.app)) as client:
        result = await (await client.post("/api/journal", headers=headers(), json={})).json()
        assert len(result["messages"]) == 50 and result["next"]
        second = await (
            await client.post("/api/journal", headers=headers(), json={"before": result["next"]})
        ).json()
        assert len(second["messages"]) == 10 and second["next"] is None
        result = await (
            await client.post("/api/journal", headers=headers(), json={"deleted_only": True})
        ).json()
        assert len(result["messages"]) == 1 and result["messages"][0]["text"] == "Updated"
        history = await (
            await client.post(
                "/api/history", headers=headers(), json={"source_id": source, "message_id": 1}
            )
        ).json()
        assert [e["kind"] for e in history["events"]] == ["deleted", "edited", "created"]
        assert history["events"][-1]["snapshot"]["text"] == item["text"]


async def test_preview_is_background_work_and_duplicate_is_rejected(miniapp):
    gate = asyncio.Event()

    async def prepare():
        await gate.wait()
        return 42

    miniapp.controls.digests.manual = prepare
    async with TestClient(TestServer(miniapp.app)) as client:
        assert (await client.post("/api/digest", headers=headers(), json={})).status == 202
        assert (await client.post("/api/digest", headers=headers(), json={})).status == 409
        state = await (await client.post("/api/state", headers=headers(), json={})).json()
        assert state["busy"]
        gate.set()
        await miniapp.digest_task
        assert miniapp.store.get("manual_job") == {"state": "queued", "run_id": 42}


async def test_guest_settings_only_expose_granted_sources_and_require_active_subscription(
    miniapp, store
):
    allowed, hidden = -1000000000123, -1000000000456
    store.sync_folder(
        [{"id": allowed, "title": "Allowed"}, {"id": hidden, "title": "Hidden"}], utcnow()
    )
    assert store.join(456, store.create_invite({allowed}))
    guest = headers(user=456)
    async with TestClient(TestServer(miniapp.app)) as client:
        path = "/api/preferences/state"
        response = await client.post(path, headers=guest, json={})
        data = await response.json()
        assert [s["title"] for s in data["sources"]] == ["Allowed"]
        path_save = "/api/preferences/save"
        revision = data["revision"]
        response = await client.post(
            path_save, headers=guest, json={"revision": revision, "source_ids": [hidden]}
        )
        assert response.status == 400
        response = await client.post(
            path_save, headers=guest, json={"revision": revision, "source_ids": []}
        )
        assert response.status == 200
        assert not store.selected_sources(456)
        assert store.selected_sources(None) == {allowed, hidden}
        for route in ("/api/state", "/api/journal", "/api/digest", "/api/settings"):
            assert (await client.post(route, headers=guest, json={})).status == 401
        assert store.revoke(456)
        assert (await client.post(path, headers=guest, json={})).status == 401


async def test_calendar_week_request_uses_local_dates_and_exposes_elapsed_start(
    miniapp, store, monkeypatch
):
    from datetime import UTC, datetime

    from ufifeed.timeutil import parse

    now = datetime(2026, 11, 5, 20, tzinfo=UTC)
    monkeypatch.setattr("ufifeed.miniapp.utcnow", lambda: now)
    gate = asyncio.Event()
    expected = (datetime(2026, 10, 26, 7, tzinfo=UTC), datetime(2026, 11, 2, 8, tzinfo=UTC))

    async def prepare(window):
        assert window == expected
        await gate.wait()
        return 99

    miniapp.controls.digests.manual = prepare
    async with TestClient(TestServer(miniapp.app)) as client:
        for invalid in (
            {"period": "day"},
            {"period": "week", "date": "2026-02-30"},
            {"period": "day", "date": "2026-11-06"},
        ):
            response = await client.post("/api/digest", headers=headers(), json=invalid)
            assert response.status == 400
        response = await client.post(
            "/api/digest", headers=headers(), json={"period": "week", "date": "2026-11-01"}
        )
        assert response.status == 202
        response = await client.post("/api/state", headers=headers(), json={})
        state = await response.json()
        assert state["digest_today"] == "2026-11-05"
        assert state["busy"] and parse(state["manual_job"]["requested_at"]) == now
        gate.set()
        await miniapp.digest_task
        assert store.get("manual_job")["run_id"] == 99
