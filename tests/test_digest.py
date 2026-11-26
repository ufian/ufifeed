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
        {"text": "Product release", "date": "2026-11-24", "id": 1},
        {"text": "PRODUCT   release", "date": "2026-11-25", "id": 2},
        {"text": "Release #ad", "date": "2026-11-25", "id": 3},
        {"text": "Weather", "date": "2026-11-25", "id": 4},
    ]
    assert [row["id"] for row in select_items(rows, ["release"], ["#ad"], 10)] == [1]


def test_html_and_emoji_parts_stay_below_telegram_limit():
    escaped = escaped_clip('<a href="bad">&🚀' * 1000, 1500)
    assert "<a" not in escaped
    assert units(escaped) <= 1500
    entries = [escaped + "\n" + '<a href="https://t.me/source/1">Source</a>'] * 30
    parts = render_parts("<b>Digest</b>", entries)
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
                            "content": '{"items":[{"text":"Brief summary","refs":[1]}]}',
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
    assert "Brief summary" in parts[0]
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
                            {"message": {"content": '{"items":[{"text":"Summary","refs":[999]}]}'}}
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
    with pytest.raises(DigestError, match="AI access"):
        await summarize(config, credentials, store, [{"ai_allowed": False}])
    assert store.reserve_ai_call(1)
    assert not store.reserve_ai_call(1)


async def test_manual_digest_keeps_automatic_window(store, config, credentials, item):
    store.upsert_live(-1000000000123, item)
    before = store.last_window_end()
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=True))
    await Digests(config, credentials, store, reader).manual()
    assert store.last_window_end() == before
    assert store.claim()["payload"].startswith("<b>Highlights · preview</b>")


async def test_busy_chat_cannot_hide_other_sources_from_ai_input(
    store,
    config,
    credentials,
    item,
    monkeypatch,
):
    import json
    from datetime import timedelta

    config.ai.enabled, config.ai.model = True, "test-model"
    config.ai.max_input_chars = 2000
    config.ai.daily_call_limit = 1000
    config.digest.max_items = 1  # Output count must not limit input coverage.
    store.set("mode", "ai")
    store.sync_folder(
        [
            {"id": -1000000000123, "title": "Busy chat"},
            {"id": -1000000000456, "title": "Quiet channel"},
        ],
        item["date"] - timedelta(hours=1),
    )
    with store.conn:
        store.conn.execute("UPDATE sources SET ai_allowed=1")
    for index in range(20):
        store.upsert_live(
            -1000000000123,
            {**item, "id": index + 1, "text": f"Busy {index}: " + 'quote "value" ' * 500},
        )
    store.upsert_live(
        -1000000000456,
        {
            **item,
            "id": 100,
            "date": item["date"] - timedelta(minutes=30),
            "text": 'Quiet "release" ' * 500,
        },
    )
    requests = []

    def handler(request):
        data = json.loads(request.content)
        records_json = data["messages"][1]["content"]
        assert len(records_json) <= config.ai.max_input_chars
        records = json.loads(records_json)
        requests.extend(records)
        ref = records[0].get("refs", [records[0].get("ref")])[0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"items": [{"text": "Summary", "refs": [ref]}]}),
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
    await digests.build(item["date"] - timedelta(hours=1), utcnow(), preview=True)
    assert {record["source"] for record in requests if "source" in record} == {
        "Busy chat",
        "Quiet channel",
    }
    assert store.get("last_ai_input")["sources"] == 2


def test_batches_keep_every_character_and_all_messages():
    from ufifeed.digest import batches, select_ai_items

    rows = [
        {"id": i, "text": f"{i} " + 'quoted "😀" ' * 200, "source_id": i % 5} for i in range(501)
    ]
    assert len(select_ai_items(rows, [], [])) == 501
    records = [{"ref": r["id"], "text": r["text"]} for r in rows]
    chunks = batches(records, 1000)
    import json

    assert all(len(json.dumps(chunk, ensure_ascii=False)) <= 1000 for chunk in chunks)
    for record in records:
        assert (
            "".join(
                part["text"] for chunk in chunks for part in chunk if part["ref"] == record["ref"]
            )
            == record["text"]
        )


async def test_world_and_each_group_have_isolated_summaries_with_reactions_and_context(
    store, config, credentials, item, monkeypatch
):
    import json
    from datetime import timedelta

    config.ai.enabled, config.ai.provider = True, "codex"
    store.set("mode", "ai")
    sources = [
        {"id": -1000000000123, "title": "World A"},
        {"id": -1000000000456, "title": "World B"},
        {"id": -42, "title": "Chat A", "kind": "group", "peer_type": "chat"},
        {"id": -43, "title": "Chat B", "kind": "group", "peer_type": "chat"},
    ]
    store.sync_folder(sources, item["date"])
    with store.conn:
        store.conn.execute("UPDATE sources SET ai_allowed=1")
    for source in sources:
        store.upsert_live(
            source["id"],
            {
                **item,
                "text": source["title"],
                "sender_id": 42,
                "reply_to": 9 if source["id"] == -42 else None,
                "reactions": [],
                "reaction_count": 80,
            },
        )
    store.upsert_live(
        -42,
        {**item, "id": 9, "date": item["date"] - timedelta(days=2), "text": "Original question"},
    )
    store.delete_messages(-1000000000456, [item["id"]])
    calls = []

    async def complete(ai, instructions, records, schema):
        calls.append((instructions, records))
        return json.dumps({"items": [{"text": "Summary", "refs": [records[0]["ref"]]}]})

    monkeypatch.setattr("ufifeed.digest.codex_complete", complete)
    parts = await Digests(config, credentials, store, None).build(
        item["date"], utcnow(), preview=True
    )
    assert len(calls) == 3
    assert {r["source"] for r in calls[0][1]} == {"World A", "World B"}
    assert "overall picture" in calls[0][0]
    assert any(r["deleted"] for r in calls[0][1])
    chats = {records[0]["source"]: (prompt, records) for prompt, records in calls[1:]}
    prompt, records = chats["Chat A"]
    assert "organic reactions" in prompt and records[0]["reactions"] == 80
    assert any(r["text"] == "Original question" and r["context_only"] for r in records)
    assert {r["source"] for r in records} == {"Chat A"}
    assert {r["source"] for r in chats["Chat B"][1]} == {"Chat B"}
    assert len(parts) == 3 and "World events" in parts[0]
    assert store.get("last_ai_input")["messages"] == 4


async def test_chunk_analysis_resumes_from_encrypted_cache_after_failure(
    store, config, credentials, item, monkeypatch
):
    import json

    config.ai.enabled, config.ai.provider = True, "codex"
    config.ai.max_input_chars = 1500
    rows = [
        {
            **item,
            "id": i,
            "date": item["date"].isoformat(),
            "title": "Source",
            "source_id": -42,
            "ai_allowed": True,
            "text": str(i) + "x" * 1500,
        }
        for i in range(3)
    ]
    calls = []
    fail = True

    async def complete(ai, instructions, records, schema):
        nonlocal fail
        calls.append(records)
        if len(calls) == 2 and fail:
            from ufifeed.codex_provider import CodexError

            fail = False
            raise CodexError("Temporary failure")
        ref = records[0].get("refs", [records[0].get("ref")])[0]
        return json.dumps({"items": [{"text": "Finding", "refs": [ref]}]})

    monkeypatch.setattr("ufifeed.digest.codex_complete", complete)
    with pytest.raises(DigestError, match="Temporary failure"):
        await summarize(config, credentials, store, rows)
    first = calls[0]
    result = await summarize(config, credentials, store, rows)
    assert result and calls.count(first) == 1
    assert all(
        b"Finding" not in row[0] for row in store.conn.execute("SELECT payload FROM analysis_cache")
    )


async def test_group_briefings_are_brief_and_details_are_collapsible(
    store, config, credentials, item, monkeypatch
):
    import json

    config.ai.enabled, config.ai.provider = True, "codex"
    store.set("mode", "ai")
    store.sync_folder(
        [{"id": -42, "title": "Discussion", "kind": "group", "peer_type": "chat"}], item["date"]
    )
    store.upsert_live(-42, item)
    with store.conn:
        store.conn.execute("UPDATE sources SET ai_allowed=1")

    async def complete(ai, instructions, records, schema):
        assert "No introductory framing" in instructions
        assert schema["properties"]["items"]["maxItems"] <= 3
        assert schema["$defs"]["AISummary"]["properties"]["text"]["maxLength"] == 320
        return json.dumps(
            {
                "items": [
                    {
                        "title": "Transit changes divided the group",
                        "text": "Evening frequency mattered most; "
                        "residents disagreed about the proposed route.",
                        "refs": [1],
                    }
                ]
            }
        )

    monkeypatch.setattr("ufifeed.digest.codex_complete", complete)
    parts = await Digests(config, credentials, store, None).build(
        item["date"], utcnow(), preview=True
    )
    assert "<b>Transit changes divided the group</b>\n<blockquote expandable>" in parts[0]
    assert "</blockquote>" in parts[0]
