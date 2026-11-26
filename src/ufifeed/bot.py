from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import BotCommand, LinkPreviewOptions, Message

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
/topics word, phrase — topics; off includes all topics
/exclude word, phrase — exclusions; off clears them
/schedule 09:00,20:00 America/Los_Angeles — digest times
/schedule — current schedule
/language en|ru — digest language
/pause /resume — disable/enable scheduled digests
/digest — preview now, without advancing the schedule
/mode extractive|ai — excerpts or AI summaries
/ai_source ID on|off — AI access for a source
/status — service status and errors
/retry ID — retry a failed or uncertain delivery
/purge ID — delete retained source data

Use explicitly selected channels and groups, without automatic categories or unread filters.
AI access sends source text to the configured AI service. Enable it for sources with an established
basis for this processing. New sources have AI access disabled.
"""


def authorized(message: Message, owner_id: int) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == owner_id
        and message.chat.type == "private"
        and message.chat.id == owner_id
    )


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and authorized(event, self.owner_id):
            return await handler(event, data)
        return None


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
        router.message.outer_middleware(OwnerOnly(config.owner.user_id))
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
        if not message.text:
            return
        command, _, argument = message.text.partition(" ")
        command, argument = command.split("@")[0].lower(), argument.strip()
        try:
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

    async def execute(self, command: str, argument: str) -> str:
        if command in {"/start", "/help"}:
            self.store.set("started", True)
            return HELP
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
            return (
                "\n".join(
                    f"{s['id']} — {s['title']} | AI: {'on' if s['ai_allowed'] else 'off'} | "
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
                f"Skipped personal dialogs/bots: {self.store.get('folder_skipped', 0)}\n"
                f"Skipped protected sources: {self.store.get('folder_protected', 0)}\n"
                f"Folder sync: {self.store.get('folder_synced_at') or 'not yet completed'}\n"
                f"Folder error: {self.store.get('folder_error') or 'none'}\n"
                f"Digest error: {self.store.get('digest_error') or 'none'}\n"
                f"Mode: {self.store.get('mode')}\n"
                f"Digest language: {self.store.get('language', 'en')}\n"
                f"Last AI input: {self.store.get('last_ai_input', {})}\n"
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
            owner_id,
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


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command=command, description=description)
            for command, description in [
                ("digest", "Digest now"),
                ("status", "Status"),
                ("folders", "Choose a folder"),
                ("sources", "Sources"),
                ("topics", "Topics"),
                ("schedule", "Schedule"),
                ("language", "Digest language"),
                ("pause", "Pause digests"),
                ("resume", "Resume digests"),
                ("help", "All commands"),
            ]
        ]
    )
