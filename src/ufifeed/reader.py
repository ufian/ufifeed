from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from telethon import TelegramClient, errors, events, functions, types, utils

from ufifeed.config import Config, Credentials
from ufifeed.store import Store
from ufifeed.timeutil import parse, stamp, utcnow

log = logging.getLogger(__name__)
HISTORY_INTERVAL_SECONDS = 3.2


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
            "Use explicitly selected chats without automatic categories or state filters."
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
        not isinstance(message, (types.Message, types.MessageService))
        or getattr(message, "noforwards", False)
        or getattr(message, "ttl_period", None)
    ):
        return None
    reply = getattr(message, "reply_to", None)
    reactions = getattr(message, "reactions", None)
    counts = []
    for result in getattr(reactions, "results", []):
        reaction = result.reaction
        counts.append(
            {
                "type": type(reaction).__name__,
                "emoji": getattr(reaction, "emoticon", None),
                "document_id": getattr(reaction, "document_id", None),
                "count": result.count,
            }
        )
    media = getattr(message, "media", None) or getattr(message, "action", None)
    text = getattr(message, "message", "") or ""
    if isinstance(media, types.MessageMediaPoll):
        text += "\n" + media.poll.question.text
        text += "\n" + "\n".join(answer.text.text for answer in media.poll.answers)
    return {
        "id": message.id,
        "date": message.date,
        "edited_at": getattr(message, "edit_date", None),
        "text": text,
        "sender_id": utils.get_peer_id(message.from_id) if message.from_id else None,
        "media_kind": type(media).__name__ if media else None,
        "media_id": next(
            (
                getattr(getattr(media, field, None), "id", None)
                for field in ("photo", "document", "poll")
                if getattr(getattr(media, field, None), "id", None)
            ),
            None,
        ),
        "entities": [entity.to_dict() for entity in getattr(message, "entities", []) or []],
        "grouped_id": getattr(message, "grouped_id", None),
        "reactions": counts if reactions is not None else None,
        "reaction_count": sum(r["count"] for r in counts if r["type"] != "ReactionPaid")
        if reactions is not None
        else None,
        "paid_reactions": sum(r["count"] for r in counts if r["type"] == "ReactionPaid")
        if reactions is not None
        else None,
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
        self.last_success = 0.0
        self.history_next = 0.0
        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_message, events.MessageEdited())
        self.client.add_event_handler(self.on_delete, events.MessageDeleted())
        self.client.add_event_handler(self.on_raw, events.Raw())

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ReaderError("The reader session is not authorized. Run ufifeed auth on the VM.")
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

    async def add_forwarded_channel(self, source_id: int, username: str | None) -> str:
        async with self.lock:
            folder_id = self.store.get("folder_id")
            if not folder_id:
                raise ReaderError("Choose a reader folder before forwarding a channel post.")
            retry_at = self.store.get("reader_retry_at")
            if retry_at and parse(retry_at) > utcnow():
                raise ReaderError(
                    "Telegram is rate limiting the reader. Try forwarding again later."
                )
            try:
                folder = await self.forwarding_folder(folder_id)
                already_in_folder = source_id in {
                    utils.get_peer_id(p) for p in folder_peers(folder)
                }
                try:
                    entity = await self.client.get_entity(username or source_id)
                except (ValueError, errors.ChannelPrivateError, errors.UsernameNotOccupiedError):
                    raise ReaderError(
                        "The reader account cannot open this channel. For a private channel, "
                        "join it from the reader account first, then forward the post again."
                    ) from None
                if (
                    not isinstance(entity, types.Channel)
                    or not entity.broadcast
                    or utils.get_peer_id(entity) != source_id
                ):
                    raise ReaderError("Telegram could not verify the original broadcast channel.")
                if entity.noforwards:
                    raise ReaderError("This channel does not allow forwarding.")
                if self.store.get("folder_id") != folder_id:
                    raise ReaderError("The reader folder changed. Forward the post again.")
                if already_in_folder and not entity.left:
                    return "This channel is already in the reader folder."
                peer = utils.get_input_peer(entity)
                if entity.left:
                    await self.client(
                        functions.channels.JoinChannelRequest(utils.get_input_channel(entity))
                    )
                muted = await self.client(
                    functions.account.UpdateNotifySettingsRequest(
                        types.InputNotifyPeer(peer),
                        types.InputPeerNotifySettings(
                            mute_until=datetime.fromtimestamp(2147483647, UTC)
                        ),
                    )
                )
                if not muted:
                    raise ReaderError("The channel is accessible, but muting failed. Try again.")
                # Fetch the latest folder after joining and muting to preserve unrelated edits.
                folder = await self.forwarding_folder(folder_id)
                if source_id not in {utils.get_peer_id(item) for item in folder_peers(folder)}:
                    folder.exclude_peers = [
                        item
                        for item in folder.exclude_peers
                        if utils.get_peer_id(item) != source_id
                    ]
                    folder.include_peers = [
                        item
                        for item in folder.include_peers
                        if utils.get_peer_id(item) != source_id
                    ] + [peer]
                    if not await self.client(
                        functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=folder)
                    ):
                        raise ReaderError(
                            "The channel is muted, but adding it to the folder failed."
                        )
                self.wake.set()
                return "Channel added to the reader folder. Notifications are muted."
            except errors.InviteRequestSentError:
                return "Join request sent. After approval, forward a post again to add the channel."
            except errors.FloodWaitError as exc:
                self.flood_pause(exc.seconds)
                raise ReaderError(
                    f"Telegram requested a {exc.seconds}-second pause. "
                    "Forward the post again later."
                ) from None

    async def forwarding_folder(self, folder_id: int):
        folders = await self.folders()
        if self.store.get("folder_id") != folder_id:
            raise ReaderError("The reader folder changed. Forward the post again.")
        folder = next((item["filter"] for item in folders if item["id"] == folder_id), None)
        if not isinstance(folder, types.DialogFilter):
            raise ReaderError("Choose a regular reader folder that the account can edit.")
        folder_peers(folder)
        return folder

    async def refresh_folder(self) -> None:
        folder_id = self.store.get("folder_id")
        if not folder_id:
            return
        folders = await self.folders()
        selected = next((item for item in folders if item["id"] == folder_id), None)
        if selected is None:
            raise ReaderError("Selected folder not found. Use /folders and /folder ID.")
        resolved = []
        entities = {}
        skipped = 0
        protected = 0
        for peer in folder_peers(selected["filter"]):
            entity = await self.client.get_entity(peer)
            # Personal dialogs and bots are deliberately outside this MVP.
            if not isinstance(entity, (types.Channel, types.Chat)):
                skipped += 1
                continue
            if getattr(entity, "noforwards", False):
                protected += 1
                continue
            source_id = utils.get_peer_id(entity)
            entities[source_id] = entity
            resolved.append(
                {
                    "id": source_id,
                    "title": entity.title,
                    "username": getattr(entity, "username", None),
                    "kind": "group"
                    if isinstance(entity, types.Chat)
                    or getattr(entity, "megagroup", False)
                    or getattr(entity, "gigagroup", False)
                    else "channel",
                    "peer_type": "chat" if isinstance(entity, types.Chat) else "channel",
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
        self.store.set("folder_protected", protected)

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
            self.store.redact(source_id, event.id)

    async def on_raw(self, update: Any) -> None:
        if isinstance(
            update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)
        ) and isinstance(update.message, types.MessageService):
            message = update.message
            await self.on_message(
                SimpleNamespace(
                    chat_id=utils.get_peer_id(message.peer_id), message=message, id=message.id
                )
            )
        if not isinstance(update, types.UpdateMessageReactions):
            return
        source_id = utils.get_peer_id(update.peer)
        if source_id not in {s["id"] for s in self.store.sources()}:
            return
        # Persist reaction-only updates without waiting for the next history pass.
        row = self.store.conn.execute(
            "SELECT * FROM messages WHERE source_id=? AND id=?", (source_id, update.msg_id)
        ).fetchone()
        if row is None or row["deleted"]:
            return
        details = json.loads(self.store.decrypt(row["details"])) if row["details"] else {}
        counts = [
            {
                "type": type(r.reaction).__name__,
                "emoji": getattr(r.reaction, "emoticon", None),
                "document_id": getattr(r.reaction, "document_id", None),
                "count": r.count,
            }
            for r in update.reactions.results
        ]
        self.store.upsert_live(
            source_id,
            {
                **details,
                "id": row["id"],
                "date": parse(row["date"]),
                "edited_at": parse(row["edited_at"]) if row["edited_at"] else None,
                "text": self.store.decrypt(row["text"]),
                "reply_to": row["reply_to"],
                "topic_id": row["topic_id"],
                "reactions": counts,
                "reaction_count": sum(r["count"] for r in counts if r["type"] != "ReactionPaid"),
                "paid_reactions": sum(r["count"] for r in counts if r["type"] == "ReactionPaid"),
            },
        )

    async def on_delete(self, event: Any) -> None:
        active = {s["id"] for s in self.store.sources()}
        if event.chat_id in active:
            self.store.delete_messages(event.chat_id, event.deleted_ids)
        elif event.chat_id is None:
            for msg_id in event.deleted_ids:
                # Unscoped IDs belong to basic chats/private dialogs, never channel ID spaces.
                matches = self.store.conn.execute(
                    "SELECT m.source_id FROM messages m JOIN sources s ON s.id=m.source_id "
                    "WHERE s.enabled=1 AND s.peer_type='chat' AND m.id=?",
                    (msg_id,),
                ).fetchall()
                if len(matches) == 1:
                    self.store.delete_messages(matches[0][0], [msg_id])

    async def history_pause(self) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self.history_next - loop.time()))
        self.history_next = loop.time() + HISTORY_INTERVAL_SECONDS

    def flood_pause(self, seconds: int) -> None:
        self.blocked_until = asyncio.get_running_loop().time() + seconds
        self.store.set("reader_retry_at", stamp(utcnow() + timedelta(seconds=seconds)))

    async def reconcile(self, source: dict) -> None:
        source_id = source["id"]
        entity = self.entities.get(source_id) or await self.client.get_entity(source_id)
        if getattr(entity, "noforwards", False):
            self.store.purge(source_id)
            raise ReaderError("This source has protected content; collection is disabled.")
        started = utcnow()
        cursor = source["cursor"]
        await self.history_pause()
        newest = await self.client.get_messages(entity, limit=1)
        snapshot_id = newest[0].id if newest else cursor
        kwargs: dict[str, Any] = {
            "reverse": True,
            "limit": None,
            "wait_time": HISTORY_INTERVAL_SECONDS,
        }
        kwargs["max_id"] = snapshot_id + 1
        if source["initialized"] or cursor:
            kwargs["min_id"] = cursor
        else:
            kwargs["offset_date"] = parse(source["since"])
        page, seen = [], 0
        await self.history_pause()
        async for message in self.client.iter_messages(entity, **kwargs):
            if not any(s["id"] == source_id for s in self.store.sources()):
                return
            cursor = max(cursor, message.id)
            data = message_data(message)
            if data:
                page.append(data)
            else:
                self.store.redact(source_id, message.id)
            seen += 1
            if seen % 100 == 0:
                self.store.commit_page(source_id, page, cursor)
                page = []
                await asyncio.sleep(0)
        self.store.commit_page(source_id, page, max(cursor, snapshot_id))
        self.history_next = asyncio.get_running_loop().time() + HISTORY_INTERVAL_SECONDS
        # Re-read a bounded recent overlap for edits/deletions missed while offline.
        await self.history_pause()
        recent = [
            m
            async for m in self.client.iter_messages(
                entity, limit=100, wait_time=HISTORY_INTERVAL_SECONDS
            )
        ]
        self.history_next = asyncio.get_running_loop().time() + HISTORY_INTERVAL_SECONDS
        refreshed = []
        for message in recent:
            data = message_data(message)
            if data and data["date"] >= parse(source["since"]):
                refreshed.append(data)
            elif not data:
                self.store.redact(source_id, message.id)
        self.store.refresh_page(source_id, refreshed)
        # Detect missed deletions by querying known IDs, never by interpreting history gaps.
        ids, verification_cursor = self.store.verification_ids(source_id)
        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            found = await self.client.get_messages(entity, ids=batch)
            # A malformed/partial result must not turn into false deletion records.
            if len(found) != len(batch):
                raise ReaderError("Incomplete message verification response")
            if any(
                message is not None
                and not isinstance(message, types.MessageEmpty)
                and message.id != msg_id
                for msg_id, message in zip(batch, found, strict=True)
            ):
                raise ReaderError("Unexpected message verification ID")
            refreshed, deleted = [], []
            for msg_id, message in zip(batch, found, strict=True):
                if message is None or isinstance(message, types.MessageEmpty):
                    deleted.append(msg_id)
                elif data := message_data(message):
                    refreshed.append(data)
                else:
                    self.store.redact(source_id, msg_id)
            self.store.refresh_page(source_id, refreshed)
            self.store.delete_messages(source_id, deleted)
            await asyncio.sleep(0)
        self.store.set(f"verify:{source_id}", verification_cursor)
        self.store.synced(source_id, started)

    async def sync_all(self) -> bool:
        loop = asyncio.get_running_loop()
        requested = loop.time()
        async with self.lock:
            if (
                self.last_success >= requested
                and not self.store.get("folder_error")
                and self.store.get("folder_synced_at")
                and all(s["initialized"] and not s["error"] for s in self.store.sources())
            ):
                return True
            retry_at = self.store.get("reader_retry_at")
            if retry_at and parse(retry_at) > utcnow():
                return False
            if retry_at:
                self.store.set("reader_retry_at", None)
            if self.blocked_until > loop.time():
                return False
            try:
                await self.refresh_folder()
            except errors.FloodWaitError as exc:
                self.flood_pause(exc.seconds)
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
                    self.flood_pause(exc.seconds)
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
            if okay:
                self.last_success = self.last_cycle
                self.store.set("reader_retry_at", None)
            return okay

    async def run(self) -> None:
        while True:
            self.wake.clear()
            await self.sync_all()
            delay = self.config.telegram.reconcile_interval_seconds
            retry_at = self.store.get("reader_retry_at")
            if retry_at:
                delay = min(delay, max(1, (parse(retry_at) - utcnow()).total_seconds() + 1))
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass
