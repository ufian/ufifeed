import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ufifeed.bot import Controls
from ufifeed.digest import DigestError, Digests
from ufifeed.language import LANGUAGES, digest_labels
from ufifeed.store import Store
from ufifeed.timeutil import utcnow


def controls_for(config, credentials, store):
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=True))
    digests = Digests(config, credentials, store, reader)
    return Controls(config, store, reader, digests), digests


async def test_language_selected_in_bot_persists_after_restart(store, config, credentials, item):
    controls, digests = controls_for(config, credentials, store)
    response = await controls.execute("/language", "ru")
    assert "Russian (ru)" in response
    reopened = Store.open(config)
    try:
        reopened.bootstrap(config)
        assert reopened.get("language") == "ru"
    finally:
        reopened.close()
    store.upsert_live(-1000000000123, item)
    parts = await digests.build(item["date"], utcnow(), preview=True)
    labels = digest_labels("ru")
    assert parts[0].startswith(f"<b>{labels['news_other']}</b>\n")
    assert parts[0].splitlines()[1].endswith(labels["manual_suffix"])
    assert item["text"] in parts[0]  # Extractive excerpts keep the original source language.


async def test_unsupported_language_does_not_change_preference(store, config, credentials):
    controls, _ = controls_for(config, credentials, store)
    store.set("language", "ru")
    response = await controls.execute("/language", "../../secrets/data.key")
    assert "Unsupported" in response
    assert store.get("language") == "ru"


async def test_existing_database_without_language_uses_english(store, config, credentials, item):
    with store.conn:
        store.conn.execute("DELETE FROM meta WHERE key='language'")
    _, digests = controls_for(config, credentials, store)
    parts = await digests.build(item["date"], utcnow(), preview=True)
    assert digest_labels("en")["empty"] in parts[0]


async def test_client_preference_sets_ai_output_language(
    store, config, credentials, item, monkeypatch
):
    config.ai.enabled, config.ai.model = True, "test-model"
    store.set("mode", "ai")
    with store.conn:
        store.conn.execute("UPDATE sources SET ai_allowed=1")
    store.upsert_live(-1000000000123, item)
    controls, digests = controls_for(config, credentials, store)
    await controls.execute("/language", "ru")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        content = json.dumps({"items": [{"text": "A test summary", "refs": [1]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "ufifeed.digest.httpx.AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    await digests.build(item["date"], utcnow(), preview=True)
    assert "in Russian" in requests[0]["messages"][0]["content"]
    await controls.execute("/language", "en")
    await digests.build(item["date"], utcnow(), preview=True)
    assert "in English" in requests[1]["messages"][0]["content"]


async def test_language_change_during_ai_request_does_not_queue_wrong_language(
    store,
    config,
    credentials,
    item,
    monkeypatch,
):
    config.ai.enabled, config.ai.model = True, "test-model"
    store.set("mode", "ai")
    with store.conn:
        store.conn.execute("UPDATE sources SET ai_allowed=1")
    store.upsert_live(-1000000000123, item)
    controls, digests = controls_for(config, credentials, store)

    async def handler(request):
        await controls.execute("/language", "ru")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"items":[{"text":"Old-language summary","refs":[1]}]}',
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
    with pytest.raises(DigestError, match="Settings changed"):
        await digests.manual()
    assert not store.delivery_status()


def test_translation_resources_have_matching_keys():
    english = digest_labels("en")
    for language in LANGUAGES:
        labels = digest_labels(language)
        assert labels.keys() == english.keys()
        assert all(labels.values())
