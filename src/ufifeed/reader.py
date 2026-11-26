from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from telethon import TelegramClient, errors, events, functions, types, utils

from ufifeed.config import Config, Credentials
from ufifeed.store import Store
from ufifeed.timeutil import parse, utcnow

log = logging.getLogger(__name__)


class ReaderError(Exception):
    pass


def folder_peers(folder: Any) -> list[Any]:
    # Deliberately require a stable, explicit reading list. Dynamic unread/muted rules
    # would silently add/remove sources as other clients change dialog state.
    dynamic = (
        "contacts",
        "non_contacts",
        "groups",
        "broadcasts",
        "bots",
        "exclude_muted",
        "exclude_read",
        "exclude_archived",
    )
    if any(getattr(folder, flag, False) for flag in dynamic):
        raise ReaderError(
            "В папке нужны явно выбранные чаты, без автокатегорий и фильтров состояния."
        )
    excluded = {utils.get_peer_id(peer) for peer in getattr(folder, "exclude_peers", [])}
    peers = [*getattr(folder, "pinned_peers", []), *getattr(folder, "include_peers", [])]
    return list(
        {
            utils.get_peer_id(peer): peer
            for peer in peers
            if utils.get_peer_id(peer) not in excluded
        }.values()
    )


def message_data(message: Any) -> dict | None:
    if (
        not isinstance(message, types.Message)
        or not message.message
        or getattr(message, "noforwards", False)
        or getattr(message, "ttl_period", None)
    ):
        return None
    reply = message.reply_to
    return {
        "id": message.id,
        "date": message.date,
        "edited_at": message.edit_date,
        "text": message.message,
        "reply_to": getattr(reply, "reply_to_msg_id", None),
        "topic_id": (
            getattr(reply, "reply_to_top_id", None)
            or (
                getattr(reply, "reply_to_msg_id", None)
                if getattr(reply, "forum_topic", False)
                else None
            )
        ),
    }


class Reader:
    def __init__(self, config: Config, credentials: Credentials, store: Store):
        self.config, self.store = config, store
        self.client = TelegramClient(
            str(config.telegram.session_path),
            credentials.api_id,
            credentials.api_hash.get_secret_value(),
            flood_sleep_threshold=0,
            request_retries=1,
            sequential_updates=True,
        )
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.entities: dict[int, Any] = {}
        self.blocked_until = 0.0
        self.last_cycle = 0.0
        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_message, events.MessageEdited())
        self.client.add_event_handler(self.on_delete, events.MessageDeleted())

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ReaderError("Сессия не авторизована. Выполните ufifeed auth на VM.")
        # Cache entities for private peer IDs, without joining any chats.
        async for _ in self.client.iter_dialogs():
            pass

    async def folders(self) -> list[dict]:
        result = await self.client(functions.messages.GetDialogFiltersRequest())
        return [
            {
                "id": folder.id,
                "title": getattr(folder.title, "text", folder.title),
                "filter": folder,
            }
            for folder in getattr(result, "filters", result)
            if getattr(folder, "id", 0) > 0 and hasattr(folder, "include_peers")
        ]

    async def refresh_folder(self) -> None:
        folder_id = self.store.get("folder_id")
        if not folder_id:
            return
        folders = await self.folders()
        selected = next((item for item in folders if item["id"] == folder_id), None)
        if selected is None:
            raise ReaderError("Выбранная папка не найдена. Используйте /folders и /folder ID.")
        resolved = []
        entities = {}
        skipped = 0
        for peer in folder_peers(selected["filter"]):
            entity = await self.client.get_entity(peer)
            # Personal dialogs and bots are deliberately outside this MVP.
            if not isinstance(entity, (types.Channel, types.Chat)):
                skipped += 1
                continue
            source_id = utils.get_peer_id(entity)
            entities[source_id] = entity
            resolved.append(
                {
                    "id": source_id,
                    "title": entity.title,
                    "username": getattr(entity, "username", None),
                }
            )
        # A command can change the folder while the above network calls are in flight.
        if self.store.get("folder_id") != folder_id:
            return
        self.entities = entities
        self.store.sync_folder(
            resolved,
            utcnow()
            - timedelta(
                hours=self.config.telegram.initial_history_hours,
            ),
        )
        self.store.set("folder_title", selected["title"])
        self.store.set("folder_skipped", skipped)

    async def on_message(self, event: Any) -> None:
        source_id = event.chat_id
        active = {source["id"] for source in self.store.sources()}
        if source_id not in active:
            return
        entity = self.entities.get(source_id)
        if getattr(entity, "noforwards", False):
            return
        data = message_data(event.message)
        if data:
            self.store.upsert_live(source_id, data)
        else:
            self.store.delete_messages(source_id, [event.id])

    async def on_delete(self, event: Any) -> None:
        # Small-group deletion updates may omit chat_id. Don't guess an ambiguous peer.
        if event.chat_id is not None:
            self.store.delete_messages(event.chat_id, event.deleted_ids)

    async def reconcile(self, source: dict) -> None:
        source_id = source["id"]
        entity = self.entities.get(source_id) or await self.client.get_entity(source_id)
        if getattr(entity, "noforwards", False):
            self.store.purge(source_id)
            raise ReaderError("Источник защищён от сохранения; сбор выключен.")
        started = utcnow()
        cursor = source["cursor"]
        newest = await self.client.get_messages(entity, limit=1)
        snapshot_id = newest[0].id if newest else cursor
        kwargs: dict[str, Any] = {"reverse": True, "limit": None}
        kwargs["max_id"] = snapshot_id + 1
        if source["initialized"] or cursor:
            kwargs["min_id"] = cursor
        else:
            kwargs["offset_date"] = parse(source["since"])
        page, seen = [], 0
        async for message in self.client.iter_messages(entity, **kwargs):
            if not any(s["id"] == source_id for s in self.store.sources()):
                return
            cursor = max(cursor, message.id)
            data = message_data(message)
            if data:
                page.append(data)
            seen += 1
            if seen % 100 == 0:
                self.store.commit_page(source_id, page, cursor)
                page = []
                await asyncio.sleep(0)
        self.store.commit_page(source_id, page, max(cursor, snapshot_id))
        # Re-read a bounded recent overlap for edits/deletions missed while offline.
        recent = [m async for m in self.client.iter_messages(entity, limit=100)]
        for message in recent:
            data = message_data(message)
            if data and data["date"] >= parse(source["since"]):
                self.store.upsert_live(source_id, data)
            else:
                self.store.delete_messages(source_id, [message.id])
        self.store.synced(source_id, started)

    async def sync_all(self) -> bool:
        async with self.lock:
            loop = asyncio.get_running_loop()
            if self.blocked_until > loop.time():
                return False
            try:
                await self.refresh_folder()
            except errors.FloodWaitError as exc:
                self.blocked_until = loop.time() + exc.seconds
                self.store.set("folder_error", f"FloodWait: {exc.seconds}s")
                return False
            except ReaderError as exc:
                self.store.set("folder_error", str(exc))
                return False
            except Exception as exc:
                self.store.set("folder_error", type(exc).__name__)
                log.warning("Folder sync failed: %s", type(exc).__name__)
                return False
            okay = bool(
                self.store.get("folder_id")
                and self.store.get("folder_synced_at")
                and not self.store.get("folder_error")
            )
            for source in self.store.sources():
                try:
                    await self.reconcile(source)
                except errors.FloodWaitError as exc:
                    self.blocked_until = loop.time() + exc.seconds
                    self.store.source_error(source["id"], f"FloodWait: {exc.seconds}s")
                    return False
                except ReaderError as exc:
                    self.store.source_error(source["id"], str(exc))
                    okay = False
                except Exception as exc:
                    self.store.source_error(source["id"], type(exc).__name__)
                    log.warning("Source %s sync failed: %s", source["id"], type(exc).__name__)
                    okay = False
            self.last_cycle = loop.time()
            return okay

    async def run(self) -> None:
        while True:
            self.wake.clear()
            await self.sync_all()
            try:
                await asyncio.wait_for(
                    self.wake.wait(), self.config.telegram.reconcile_interval_seconds
                )
            except TimeoutError:
                pass
