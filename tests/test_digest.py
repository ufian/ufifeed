from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ufifeed.digest import (
    DigestError,
    Digests,
    escaped_clip,
    message_link,
    render_parts,
    select_items,
    summarize,
    units,
)
from ufifeed.timeutil import utcnow


def test_selection_removes_ads_and_duplicate_posts():
    rows = [
        {"text": "Релиз продукта", "date": "2026-11-24", "id": 1},
        {"text": "РЕЛИЗ   продукта", "date": "2026-11-25", "id": 2},
        {"text": "Релиз #реклама", "date": "2026-11-25", "id": 3},
        {"text": "Погода", "date": "2026-11-25", "id": 4},
    ]
    assert [row["id"] for row in select_items(rows, ["релиз"], ["#реклама"], 10)] == [1]


def test_html_and_emoji_parts_stay_below_telegram_limit():
    escaped = escaped_clip('<a href="bad">&🚀' * 1000, 1500)
    assert "<a" not in escaped
    assert units(escaped) <= 1500
    entries = [escaped + "\n" + '<a href="https://t.me/source/1">Источник</a>'] * 30
    parts = render_parts("<b>Сводка</b>", entries)
    assert len(parts) > 1
    assert all(units(part) <= 3500 for part in parts)


def test_only_telegram_source_links_are_constructed():
    assert message_link({"source_id": -1000000000123, "id": 5}) == "https://t.me/c/123/5"
    assert message_link({"source_id": -42, "id": 5}) is None
    assert message_link({"source_id": -100123, "id": 5}) is None
    assert message_link({"username": 'x" onload="bad', "source_id": -42, "id": 5}) is None


async def test_ai_sources_filtered_before_provider_request(
    store, config, credentials, item, monkeypatch
):
    config.ai.enabled, config.ai.model = True, "test-model"
    store.set("mode", "ai")
    store.upsert_live(-1000000000123, {**item, "text": "private forbidden source"})
    store.sync_folder(
        [
            {"id": -1000000000123, "title": "Forbidden"},
            {"id": -1000000000456, "title": "Allowed"},
        ],
        item["date"],
    )
    store.conn.execute("UPDATE sources SET ai_allowed=1 WHERE id=-1000000000456")
    store.conn.commit()
    store.upsert_live(-1000000000456, {**item, "text": "allowed post"})
    requests = []

    def handler(request):
        requests.append(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"items":[{"text":"Краткий итог","refs":[1]}]}',
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "ufifeed.digest.httpx.AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    digests = Digests(
        config, credentials, store, SimpleNamespace(sync_all=AsyncMock(return_value=True))
    )
    parts = await digests.build(item["date"], utcnow(), preview=True)
    assert "Краткий итог" in parts[0]
    assert len(requests) == 1
    assert "allowed post" in requests[0]
    assert "forbidden" not in requests[0]


async def test_ai_rejects_unknown_citations_and_does_not_enqueue(
    store,
    config,
    credentials,
    item,
    monkeypatch,
):
    config.ai.enabled, config.ai.model = True, "test-model"
    store.set("mode", "ai")
    store.conn.execute("UPDATE sources SET ai_allowed=1")
    store.conn.commit()
    store.upsert_live(-1000000000123, item)
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "ufifeed.digest.httpx.AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": '{"items":[{"text":"Итог","refs":[999]}]}'}}
                        ],
                    },
                )
            ),
            **kwargs,
        ),
    )
    digests = Digests(
        config, credentials, store, SimpleNamespace(sync_all=AsyncMock(return_value=True))
    )
    before = store.last_window_end()
    with pytest.raises(DigestError):
        await digests.manual()
    assert not store.delivery_status()
    assert store.last_window_end() == before


async def test_ai_permission_check_and_daily_budget(store, config, credentials):
    config.ai.enabled, config.ai.model = True, "test-model"
    with pytest.raises(DigestError, match="AI-доступ"):
        await summarize(config, credentials, store, [{"ai_allowed": False}])
    assert store.reserve_ai_call(1)
    assert not store.reserve_ai_call(1)


async def test_manual_digest_keeps_automatic_window(store, config, credentials, item):
    store.upsert_live(-1000000000123, item)
    before = store.last_window_end()
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=True))
    await Digests(config, credentials, store, reader).manual()
    assert store.last_window_end() == before
    assert store.claim()["payload"].startswith("<b>Подборка · вручную</b>")
