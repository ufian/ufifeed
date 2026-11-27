from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from ufifeed.bot import Controls, deliver_one
from ufifeed.digest import Digests
from ufifeed.timeutil import utcnow


async def test_controls_ignore_other_users_and_groups(store, config):
    controls = Controls(config, store, None, None)
    controls.execute = AsyncMock(return_value="")
    for user_id, chat_type, chat_id in [(456, "private", 456), (123, "group", -12)]:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            text="/folder 9",
            from_user=User(id=user_id, is_bot=False, first_name="User"),
            chat=Chat(id=chat_id, type=chat_type),
        )
        await controls.handle(message)
    controls.execute.assert_not_called()
    owner = Message(
        message_id=2,
        date=datetime.now(UTC),
        text="/status",
        from_user=User(id=123, is_bot=False, first_name="Owner"),
        chat=Chat(id=123, type="private"),
    )
    await controls.handle(owner)
    controls.execute.assert_awaited_once()


async def test_network_failure_is_unknown_and_never_automatically_resent(store, item):
    store.set("started", True)
    store.enqueue("slot", item["date"], utcnow(), "extractive", ["text", "next"], advance=False)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    await deliver_one(bot, store, 123)
    await deliver_one(bot, store, 123)
    assert store.delivery_status() == {"unknown": 1, "pending": 1}
    bot.send_message.assert_awaited_once()


async def test_known_rate_limit_retries_after_requested_delay(store, item):
    store.set("started", True)
    store.enqueue("slot", item["date"], utcnow(), "extractive", ["text"], advance=False)
    error = TelegramRetryAfter(
        method=SendMessage(chat_id=123, text="text"), message="rate limited", retry_after=60
    )
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    await deliver_one(bot, store, 123)
    assert store.delivery_status() == {"pending": 1}
    assert store.claim() is None


async def test_setting_schedule_validates_time_and_seattle_zone(store, config, credentials):
    reader = SimpleNamespace(sync_all=AsyncMock(return_value=True))
    controls = Controls(config, store, reader, Digests(config, credentials, store, reader))
    result = await controls.execute("/schedule", "08:00,19:30 America/Los_Angeles")
    assert "enabled" in result
    assert store.get("schedule")["times"] == ["08:00", "19:30"]
    await controls.execute("/pause", "")
    assert not store.get("schedule")["enabled"]


async def test_journal_history_and_folder_ai_controls(store, config, credentials, item):
    from types import SimpleNamespace

    from ufifeed.bot import Controls

    controls = Controls(config, store, SimpleNamespace(), SimpleNamespace())
    store.upsert_live(-1000000000123, item)
    store.delete_messages(-1000000000123, [item["id"]])
    assert "Versions are retained" in await controls.execute("/journal", "")
    history = await controls.execute("/history", f"-1000000000123 {item['id']}")
    assert item["text"] in history and "deleted" in history
    store.set("folder_id", 214)
    await controls.execute("/ai_folder", "on")
    store.sync_folder([{"id": -42, "title": "Added"}], item["date"])
    assert store.sources()[0]["ai_allowed"]
    await controls.execute("/ai_source", "-42 off")
    store.sync_folder([{"id": -42, "title": "Added"}], item["date"])
    assert not store.sources()[0]["ai_allowed"]
    store.change_folder(5)
    assert not store.get("ai_folder")


async def test_only_owner_and_subscribers_can_add_forwarded_media_channel(
    store, config, monkeypatch
):
    import ufifeed.bot as controls_module

    reader = SimpleNamespace(add_forwarded_channel=AsyncMock(return_value="Channel added"))
    controls = Controls(config, store, reader, None)
    monkeypatch.setattr(controls_module, "reply", AsyncMock())
    assert store.join(789, store.create_invite({-1000000000123}))
    for user_id, chat_type, chat_id in [
        (456, "private", 456),
        (789, "private", 789),
        (123, "group", -12),
        (123, "private", 123),
    ]:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            from_user=User(id=user_id, is_bot=False, first_name="User"),
            chat=Chat(id=chat_id, type=chat_type),
            forward_origin={
                "type": "channel",
                "date": datetime.now(UTC),
                "message_id": 42,
                "chat": {"id": -1000000000123, "type": "channel", "username": "example"},
            },
        )
        await controls.handle(message)
    assert reader.add_forwarded_channel.await_args_list == [
        call(-1000000000123, "example", recipient_id=789),
        call(-1000000000123, "example"),
    ]


async def test_invite_message_has_a_working_link_and_manual_join_code(store, config, monkeypatch):
    import re

    import ufifeed.bot as controls_module

    controls = Controls(config, store, None, None)
    reply = AsyncMock()
    monkeypatch.setattr(controls_module, "reply", reply)
    owner = config.owner.user_id
    message = SimpleNamespace(
        text="/invite all",
        forward_origin=None,
        from_user=SimpleNamespace(id=owner, is_bot=False),
        chat=SimpleNamespace(id=owner, type="private"),
        bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="example_bot"))),
    )
    await controls.handle(message)
    instructions = reply.await_args.args[1]
    code = re.search(r"(?m)^/join ([A-Za-z0-9_-]{32})$", instructions)[1]
    assert f"https://t.me/example_bot?start=join_{code}" in instructions
    assert "tap Start" in instructions
    assert "/settings" in instructions and "24 hours" in instructions
    controls.send_settings = AsyncMock()
    assert "You are subscribed" in await controls.subscriber_command(message, 456, "/join", code)
    assert store.subscriber(456)
    assert "already used" in await controls.subscriber_command(message, 789, "/join", code)


async def test_subscriber_add_command_accepts_a_public_link(store, config):
    assert store.join(456, store.create_invite({-1000000000123}))
    reader = SimpleNamespace(add_source=AsyncMock(return_value="Source added."))
    controls = Controls(config, store, reader, None)
    await controls.subscriber_command(None, 789, "/add", "@example")
    await controls.subscriber_command(None, 456, "/add", "https://t.me/+private")
    reader.add_source.assert_not_awaited()
    assert (
        await controls.subscriber_command(None, 456, "/add", "https://t.me/example/123")
        == "Source added."
    )
    reader.add_source.assert_awaited_once_with("example", recipient_id=456)
