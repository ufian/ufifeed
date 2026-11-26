from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from ufifeed.bot import Controls, OwnerOnly, deliver_one
from ufifeed.digest import Digests
from ufifeed.timeutil import utcnow


async def test_controls_ignore_other_users_and_groups():
    middleware = OwnerOnly(123)
    handler = AsyncMock()
    for user_id, chat_type, chat_id in [(456, "private", 456), (123, "group", -12)]:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            text="/folder 9",
            from_user=User(id=user_id, is_bot=False, first_name="User"),
            chat=Chat(id=chat_id, type=chat_type),
        )
        await middleware(handler, message, {})
    handler.assert_not_called()
    owner = Message(
        message_id=2,
        date=datetime.now(UTC),
        text="/status",
        from_user=User(id=123, is_bot=False, first_name="Owner"),
        chat=Chat(id=123, type="private"),
    )
    await middleware(handler, owner, {})
    handler.assert_awaited_once()


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
    assert "включено" in result
    assert store.get("schedule")["times"] == ["08:00", "19:30"]
    await controls.execute("/pause", "")
    assert not store.get("schedule")["enabled"]
