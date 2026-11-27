from __future__ import annotations

import asyncio
import logging
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    WebAppInfo,
)

from ufifeed.config import Config, Schedule
from ufifeed.digest import DigestError, Digests, units
from ufifeed.language import LANGUAGES, language_code
from ufifeed.reader import ReaderError
from ufifeed.store import Store
from ufifeed.timeutil import next_slot, stamp, utcnow

log = logging.getLogger(__name__)

HELP = """ufifeed — private digests from a dedicated Telegram account folder.

/folders — list folders in the reader account
/folder ID — choose a folder (0 disables collection)
/sources — sources and collection status
/add @username — add a public channel or group
/subscribe all|none|ID [ID ...] — choose sources for your digest
/settings — open your digest settings
/invite ID [ID ...] — create a single-use invitation for selected sources
/invite all — invite with access to all current sources
/subscribers — list active subscribers
/revoke USER_ID — revoke a subscriber
/topics word, phrase — topics; off includes all topics
/exclude word, phrase — exclusions; off clears them
/schedule 09:00,20:00 America/Los_Angeles — digest times
/schedule — current schedule
/language en|ru — digest language
/pause /resume — disable/enable scheduled digests
/digest — preview now, without advancing the schedule
/mode extractive|ai — excerpts or AI summaries
/ai_source ID on|off — AI access for a source
/ai_folder on|off — AI access for current and future sources in this folder
/journal — message, deletion and version counts
/history SOURCE_ID MESSAGE_ID — observed versions of a message
/status — service status and errors
/retry ID — retry a failed or uncertain delivery
/purge ID — delete retained source data

Forward a channel post here to add and mute its channel in the reader folder.
Use explicitly selected channels and groups, without automatic categories or unread filters.
AI access sends source text to the configured AI service. Enable it for sources with an established
basis for this processing. New sources inherit /ai_folder (off by default).
"""


def authorized(message: Message, owner_id: int) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == owner_id
        and message.chat.type == "private"
        and message.chat.id == owner_id
    )


async def reply(message: Message, text: str) -> None:
    chunk = ""
    for line in text.splitlines(keepends=True):
        for char in line:
            if units(chunk + char) > 3500:
                await message.answer(
                    chunk,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                chunk = ""
            chunk += char
    if chunk:
        await message.answer(
            chunk, parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )


class Controls:
    def __init__(self, config: Config, store: Store, reader, digests: Digests):
        self.config, self.store, self.reader, self.digests = config, store, reader, digests
        self.dispatcher = Dispatcher()
        router = Router()
        router.message.register(self.handle)
        self.dispatcher.include_router(router)

    def schedule_text(self) -> str:
        schedule = Schedule.model_validate(self.store.get("schedule"))
        next_due = next_slot(utcnow(), schedule.times, schedule.timezone)
        return (
            f"Schedule: {'enabled' if schedule.enabled else 'disabled'}\n"
            f"{', '.join(schedule.times)} · {schedule.timezone}\n"
            f"Next slot: {next_due.astimezone(ZoneInfo(schedule.timezone)):%d.%m %H:%M %Z}"
        )

    async def handle(self, message: Message) -> None:
        if not message.from_user or message.from_user.is_bot:
            return
        user_id = message.from_user.id
        if not authorized(message, user_id):
            return
        recipient_id = None if user_id == self.config.owner.user_id else user_id
        command, _, argument = (message.text or "").partition(" ")
        command, argument = command.split("@")[0].lower(), argument.strip()
        try:
            origin = message.forward_origin
            if isinstance(origin, (MessageOriginChannel, MessageOriginChat)):
                chat = (
                    origin.chat if isinstance(origin, MessageOriginChannel) else origin.sender_chat
                )
                if recipient_id is None:
                    text = await self.reader.add_forwarded_channel(chat.id, chat.username)
                elif self.store.subscriber(recipient_id):
                    text = await self.reader.add_forwarded_channel(
                        chat.id, chat.username, recipient_id=recipient_id
                    )
                else:
                    return
            elif origin is not None:
                if recipient_id is not None and not self.store.subscriber(recipient_id):
                    return
                text = "This forward has no source chat. Use /add @username for a public source."
            elif not message.text:
                return
            elif recipient_id is not None:
                text = await self.subscriber_command(message, user_id, command, argument)
            elif command == "/invite":
                if not argument:
                    text = "Use /invite ID [ID ...] or /invite all. See /sources for IDs."
                else:
                    source_ids = self.parse_sources(argument, None)
                    bot = await message.bot.me()
                    code = self.store.create_invite(source_ids)
                    bot_url = f"https://t.me/{bot.username}"
                    text = (
                        "You're invited to subscribe to Ufifeed digests.\n\n"
                        f"1. Open {bot_url}?start=join_{code} and tap Start.\n"
                        "2. Tap Digest settings or send /settings to choose your sources.\n"
                        "3. Digests will arrive in your private chat with the bot. "
                        "Send /schedule to see delivery times.\n"
                        "To add a public source, forward a channel post or send /add @username.\n\n"
                        f"If the invite link doesn't work, open {bot_url} "
                        "and send this command in the private chat:\n"
                        f"/join {code}\n\n"
                        "This invite can be used once and expires in 24 hours."
                    )
            elif command == "/settings":
                await self.send_settings(message)
                return
            else:
                text = await self.execute(command, argument)
        except (ValueError, ReaderError, DigestError) as exc:
            # Pydantic errors can include submitted text; command arguments aren't secrets.
            text = (
                str(exc)
                if isinstance(exc, (ReaderError, DigestError))
                else "Check the command format. /help"
            )
        except Exception as exc:
            log.warning("Control command failed: %s", type(exc).__name__)
            text = f"Command failed: {type(exc).__name__}. Try again later."
        await reply(message, text)

    async def add_public_source(self, argument: str, recipient_id: int | None) -> str:
        match = re.fullmatch(
            r"(?:@|(?:https://)?t\.me/(?:s/)?)([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?",
            argument,
            re.IGNORECASE,
        )
        if not match:
            return "Use /add @username or /add https://t.me/username for a public source."
        return await self.reader.add_source(match[1], recipient_id=recipient_id)

    def parse_sources(self, argument: str, recipient_id: int | None) -> set[int]:
        if argument == "all":
            return {s["id"] for s in self.store.available_sources(recipient_id)}
        if argument == "none":
            return set()
        return {int(value) for value in argument.replace(",", " ").split()}

    def select_sources(self, recipient_id: int | None, argument: str) -> str:
        if not argument:
            return "Use /subscribe all, /subscribe none, or /subscribe ID [ID ...]."
        selected = None if argument == "all" else self.parse_sources(argument, recipient_id)
        self.store.select_sources(recipient_id, selected)
        return "Digest sources saved. See /sources."

    async def send_settings(self, message: Message) -> None:
        if not self.config.miniapp.enabled:
            await reply(message, "Use /sources, /subscribe and /language to manage your digest.")
            return
        url = self.config.miniapp.public_url.rstrip("/") + "/preferences"
        await message.answer(
            "Choose the sources for your digest.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Digest settings", web_app=WebAppInfo(url=url))]
                ]
            ),
        )

    async def subscriber_command(
        self, message: Message, user_id: int, command: str, argument: str
    ) -> str:
        code = argument.removeprefix("join_") if command == "/start" else argument
        if command == "/join" or (command == "/start" and argument.startswith("join_")):
            if not re.fullmatch(r"[A-Za-z0-9_-]{32}", code) or not self.store.join(user_id, code):
                return "This invitation is invalid, expired, or already used."
            await self.send_settings(message)
            return "You are subscribed. Digests will arrive here on the shared schedule."
        preferences = self.store.subscriber(user_id)
        if not preferences:
            return "You need an invitation to subscribe." if command == "/start" else ""
        if command == "/settings":
            await self.send_settings(message)
            return ""
        if command == "/add":
            return await self.add_public_source(argument, user_id)
        if command == "/subscribe":
            return self.select_sources(user_id, argument)
        if command == "/sources":
            active = self.store.selected_sources(user_id)
            return (
                "\n".join(
                    f"{s['id']} — {s['title']} [{'on' if s['id'] in active else 'off'}]"
                    for s in self.store.available_sources(user_id)
                )
                or "There are no available sources."
            )
        if command == "/language":
            if argument:
                self.store.subscriber_language(user_id, language_code(argument))
            return f"Digest language: {argument or preferences['language']}. Use /language en|ru."
        if command == "/schedule":
            return self.schedule_text()
        if command == "/unsubscribe":
            self.store.revoke(user_id)
            return "Subscription ended."
        return (
            "/settings — choose digest sources\n/sources — available sources\n"
            "/add @username — add a public source, or forward a channel post\n"
            "/subscribe all|none|ID [ID ...] — choose sources\n"
            "/language en|ru — digest language\n/schedule — delivery times\n"
            "/unsubscribe — stop receiving digests"
        )

    async def execute(self, command: str, argument: str) -> str:
        if command in {"/start", "/help"}:
            self.store.set("started", True)
            return HELP
        if command == "/add":
            return await self.add_public_source(argument, None)
        if command == "/subscribe":
            return self.select_sources(None, argument)
        if command == "/subscribers":
            return "\n".join(str(s["id"]) for s in self.store.subscribers()) or "No subscribers."
        if command == "/revoke":
            return "Subscriber revoked." if self.store.revoke(int(argument)) else "No subscriber."
        if command == "/folders":
            folders = await self.reader.folders()
            return (
                (
                    "Reader account folders:\n"
                    + "\n".join(f"{folder['id']} — {folder['title']}" for folder in folders)
                    + "\nSelect: /folder ID"
                )
                if folders
                else "No folders found. Create one in the reader account."
            )
        if command == "/folder":
            folder_id = int(argument)
            if folder_id < 0:
                raise ValueError("Invalid folder")
            if folder_id and not any(f["id"] == folder_id for f in await self.reader.folders()):
                return "Folder not found. Check /folders."
            self.store.change_folder(folder_id or None)
            self.reader.wake.set()
            return (
                "Folder selected; synchronization requested. /status"
                if folder_id
                else "Collection disabled."
            )
        if command == "/sources":
            sources = self.store.sources()
            selected = self.store.selected_sources(None)
            return (
                "\n".join(
                    f"{s['id']} — {s['title']} | "
                    f"Digest: {'on' if s['id'] in selected else 'off'} | "
                    f"AI: {'on' if s['ai_allowed'] else 'off'} | "
                    f"{s['error'] or ('synced' if s['initialized'] else 'pending')}"
                    for s in sources
                )
                or "No sources yet. Choose a folder using /folders."
            )
        if command in {"/topics", "/exclude"}:
            key = command[1:]
            if not argument:
                return f"{key}: {', '.join(self.store.get(key, [])) or 'none'}"
            words = (
                [] if argument == "off" else [v.strip() for v in argument.split(",") if v.strip()]
            )
            if len(words) > 50 or any(len(word) > 100 for word in words):
                raise ValueError("Too many keywords")
            self.store.set(key, words)
            return "Settings saved."
        if command == "/schedule":
            if argument:
                parts = argument.split()
                if len(parts) not in (1, 2):
                    raise ValueError("Invalid schedule")
                schedule = Schedule(
                    enabled=True,
                    times=parts[0].split(","),
                    timezone=(
                        parts[1] if len(parts) == 2 else self.store.get("schedule")["timezone"]
                    ),
                )
                self.store.set("schedule", schedule.model_dump())
                # A changed schedule starts now; resume is the command for catching up.
                self.store.set("window_end", stamp(utcnow()))
            return self.schedule_text()
        if command in {"/pause", "/resume"}:
            schedule = self.store.get("schedule")
            schedule["enabled"] = command == "/resume"
            self.store.set("schedule", schedule)
            return self.schedule_text()
        if command == "/language":
            if argument:
                try:
                    code = language_code(argument)
                except ValueError:
                    return "Unsupported digest language. Use /language en or /language ru."
                self.store.set("language", code)
            code = self.store.get("language", "en")
            choices = ", ".join(f"{value} ({key})" for key, value in LANGUAGES.items())
            return (
                f"Digest language: {LANGUAGES[code]} ({code}).\nAvailable: {choices}.\n"
                "AI summaries use this language; extractive excerpts keep the source language."
            )
        if command == "/digest":
            if self.digests.lock.locked():
                return "A digest is already being prepared. Check /status in a minute."
            run = await self.digests.manual()
            return f"Digest #{run} prepared and queued for delivery."
        if command == "/mode":
            if not argument:
                return f"Mode: {self.store.get('mode')}"
            if argument not in {"extractive", "ai"}:
                raise ValueError("Invalid mode")
            if argument == "ai" and not self.config.ai.enabled:
                return (
                    "The AI service is not configured on the server. Use extractive mode for now."
                )
            self.store.set("mode", argument)
            return f"Mode: {argument}. AI processes only sources marked AI: on in /sources."
        if command == "/journal":
            return (
                f"Encrypted journal: {self.store.journal_stats()}\n"
                f"Archive retention: {self.config.storage.retention_days or 'unlimited'} days. "
                "Versions are retained after deletion."
            )
        if command == "/history":
            source, msg_id = map(int, argument.split())
            history = self.store.message_history(source, msg_id)
            return (
                "\n\n".join(
                    f"{event['observed_at']} {event['kind']}\n"
                    f"Edited at: {event['payload'].get('edited_at') or 'unknown'}\n"
                    f"{event['payload'].get('text', '')}"
                    for event in history
                )
                or "No observed versions."
            )
        if command == "/ai_folder":
            if argument not in {"on", "off"}:
                return "Use /ai_folder on|off."
            if not self.store.get("folder_id"):
                return "Select a folder first. /folders"
            with self.store.conn:
                self.store._set("ai_folder", argument == "on")
                self.store.conn.execute(
                    "UPDATE sources SET ai_allowed=? WHERE enabled=1", (int(argument == "on"),)
                )
            if argument == "off":
                self.store.invalidate_pending()
            return f"AI access for current and future folder sources: {argument}."
        if command == "/ai_source":
            source, state = argument.split()
            source_id = int(source)
            if state not in {"on", "off"}:
                raise ValueError("Invalid state")
            if source_id not in {s["id"] for s in self.store.sources()}:
                return "Active source not found. /sources"
            with self.store.conn:
                self.store.conn.execute(
                    "UPDATE sources SET ai_allowed=? WHERE id=?", (int(state == "on"), source_id)
                )
            if state == "off":
                self.store.invalidate_pending()
            return f"AI access for source {source_id}: {state}."
        if command == "/status":
            return (
                f"Folder: {self.store.get('folder_title', 'not selected')} "
                f"(ID {self.store.get('folder_id')})\n"
                f"Sources: {len(self.store.sources())}\n"
                f"Journal: {self.store.journal_stats()}\n"
                f"Skipped personal dialogs/bots: {self.store.get('folder_skipped', 0)}\n"
                f"Skipped protected sources: {self.store.get('folder_protected', 0)}\n"
                f"Folder sync: {self.store.get('folder_synced_at') or 'not yet completed'}\n"
                f"Folder error: {self.store.get('folder_error') or 'none'}\n"
                f"Digest error: {self.store.get('digest_error') or 'none'}\n"
                f"Mode: {self.store.get('mode')}\n"
                f"Digest language: {self.store.get('language', 'en')}\n"
                f"Last AI input: {self.store.get('last_ai_input', {})}\n"
                f"AI progress: {self.store.get('ai_progress', {})}\n"
                f"Queue: {self.store.delivery_status()}\n"
                f"Delivery issues: {self.store.delivery_issues()}\n" + self.schedule_text()
            )
        if command == "/retry":
            parts = argument.split()
            if not parts:
                return "Use /retry ID. Delivery issue IDs are listed in /status."
            item_id = int(parts[0])
            if len(parts) != 2 or parts[1] != "confirm":
                return (
                    f"Retrying an unknown result may create a duplicate. "
                    f"To retry: /retry {item_id} confirm"
                )
            return "Retry queued." if self.store.retry(item_id) else "Delivery not found."
        if command == "/purge":
            parts = argument.split()
            if not parts:
                return "Use /purge ID. Source IDs are listed in /sources."
            source_id = int(parts[0])
            if len(parts) != 2 or parts[1] != "confirm":
                return (
                    "Remove the source from the folder first or it will be collected again. "
                    "This deletes collected messages and stored digest text: "
                    f"/purge {source_id} confirm"
                )
            if source_id not in {s["id"] for s in self.store.sources(active_only=False)}:
                return "Source not found."
            if source_id in {s["id"] for s in self.store.sources()}:
                return (
                    "The source is still in the folder. "
                    "Remove it there and wait for synchronization first."
                )
            self.store.purge(source_id)
            return "Stored messages and digest text deleted."
        return "Unknown command. /help"


async def deliver_one(bot: Bot, store: Store, owner_id: int) -> bool:
    if not store.get("started"):
        return False
    item = store.claim()
    if item is None:
        return False
    try:
        message = await bot.send_message(
            item["recipient_id"] or owner_id,
            item["payload"],
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramRetryAfter as exc:
        store.delivery_error(item["id"], "pending", "rate_limited", delay=exc.retry_after + 1)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        store.delivery_error(item["id"], "failed", type(exc).__name__)
    except Exception as exc:
        store.delivery_error(item["id"], "unknown", type(exc).__name__)
    else:
        store.delivered(item["id"], message.message_id)
    return True


async def delivery_loop(bot: Bot, store: Store, owner_id: int) -> None:
    while True:
        await deliver_one(bot, store, owner_id)
        await asyncio.sleep(1.1)


async def set_commands(bot: Bot, owner_id: int) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command=command, description=description)
            for command, description in [
                ("settings", "Digest settings"),
                ("invite", "Create an invitation"),
                ("subscribers", "Active subscribers"),
                ("digest", "Digest now"),
                ("status", "Status"),
                ("folders", "Choose a folder"),
                ("sources", "Sources"),
                ("add", "Add a public source"),
                ("journal", "Message journal statistics"),
                ("history", "Observed message versions"),
                ("topics", "Topics"),
                ("schedule", "Schedule"),
                ("language", "Digest language"),
                ("pause", "Pause digests"),
                ("resume", "Resume digests"),
                ("help", "All commands"),
            ]
        ],
        scope=BotCommandScopeChat(chat_id=owner_id),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="settings", description="Digest settings"),
            BotCommand(command="sources", description="Available sources"),
            BotCommand(command="add", description="Add a public source"),
            BotCommand(command="language", description="Digest language"),
            BotCommand(command="schedule", description="Delivery times"),
            BotCommand(command="help", description="All commands"),
        ]
    )
