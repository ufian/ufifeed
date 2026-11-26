from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import getpass
import json
import logging
import os
import secrets
import sys
import time
import tomllib
from pathlib import Path

from aiogram import Bot
from pydantic import ValidationError
from telethon import TelegramClient

from ufifeed.authentication import AuthError, login
from ufifeed.config import Config, Credentials
from ufifeed.digest import Digests
from ufifeed.reader import Reader
from ufifeed.store import Store, create_key
from ufifeed.timeutil import stamp, utcnow


class CLIError(Exception):
    """An explicitly safe, actionable error for the person operating the service."""


@contextlib.contextmanager
def session_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CLIError("The reader session is already in use; stop the app first") from None
        yield


async def pair_owner(bot_token: str) -> int:
    token = secrets.token_urlsafe(18)
    async with Bot(bot_token) as bot:
        user = await bot.get_me()
        if (await bot.get_webhook_info()).url:
            raise CLIError("This bot has a webhook. Create a dedicated bot in BotFather")
        print(f"In your primary Telegram account, open https://t.me/{user.username}")
        print(f"Send this command to the bot: /start {token}", flush=True)
        offset, deadline = 0, time.monotonic() + 300
        while time.monotonic() < deadline:
            for update in await bot.get_updates(
                offset=offset, timeout=20, allowed_updates=["message"]
            ):
                offset = update.update_id + 1
                msg = update.message
                if (
                    msg
                    and msg.text == f"/start {token}"
                    and msg.from_user
                    and msg.chat.type == "private"
                    and msg.chat.id == msg.from_user.id
                ):
                    return msg.from_user.id
    raise CLIError("Pairing timed out; repeat setup")


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def setup(config_path: Path, credentials_path: Path) -> None:
    if config_path.exists() or credentials_path.exists():
        raise CLIError("Config/credentials already exist. Setup will not overwrite them")
    print("Enter credentials directly in this terminal. Secret input is hidden.")
    api_id = int(input("Telegram API ID of your reader application: "))
    api_hash = getpass.getpass("Telegram API hash: ")
    bot_token = getpass.getpass("Token of your new dedicated bot from BotFather: ")
    credentials = Credentials(api_id=api_id, api_hash=api_hash, bot_token=bot_token)
    owner_id = asyncio.run(pair_owner(credentials.bot_token.get_secret_value()))
    template = Path("config/ufifeed.example.toml").read_text()
    template = template.replace("user_id = 123456789", f"user_id = {owner_id}")
    config = Config.model_validate(tomllib.loads(template))
    if not config.storage.key_path.exists():
        create_key(config.storage.key_path)
    private_write(
        credentials_path,
        json.dumps(
            {
                "api_id": api_id,
                "api_hash": api_hash,
                "bot_token": bot_token,
            }
        )
        + "\n",
    )
    private_write(config_path, template)
    print("Owner paired. Run auth, start the app, then send /start to the bot.")


async def auth(config: Config, credentials: Credentials, *, use_qr: bool = False) -> None:
    with session_lock(config.telegram.session_path):
        client = TelegramClient(
            str(config.telegram.session_path),
            credentials.api_id,
            credentials.api_hash.get_secret_value(),
            flood_sleep_threshold=0,
            request_retries=1,
            connection_retries=2,
        )
        try:
            await login(client, use_qr=use_qr)
            me = await client.get_me()
            if me.bot:
                raise CLIError("Reader must be a user account, not a bot")
            print("Reader account authorized. The session has been saved on the VM.")
        finally:
            await client.disconnect()


async def configure(config: Config, credentials: Credentials, args) -> None:
    """Bootstrap owner-requested settings offline, before the main service starts."""
    store = Store.open(config)
    store.bootstrap(config)
    reader = Reader(config, credentials, store)
    try:
        await reader.connect()
        matches = [
            folder
            for folder in await reader.folders()
            if folder["title"].casefold() == args.folder.casefold()
        ]
        if len(matches) != 1:
            raise CLIError("Folder name must match exactly one reader folder")
        folder = matches[0]
        if store.get("folder_id") != folder["id"]:
            store.change_folder(folder["id"])
        if args.language:
            store.set("language", args.language)
        if args.mode:
            if args.mode == "ai" and not config.ai.enabled:
                raise CLIError("Configure an AI provider before selecting AI mode")
            store.set("mode", args.mode)
        end = utcnow()
        if not await reader.sync_all():
            raise CLIError("Source synchronization is incomplete; inspect status and retry")
        if args.enable_ai_for_folder:
            if not config.ai.enabled:
                raise CLIError("Configure an AI provider before enabling folder AI access")
            with store.conn:
                store.conn.execute("UPDATE sources SET ai_allowed=1 WHERE enabled=1")
        print(f"Configured folder {folder['title']!r}: {len(store.sources())} sources")
        if args.queue_digest:
            slot = f"initial:{folder['id']}"
            if store.conn.execute("SELECT 1 FROM runs WHERE slot=?", (slot,)).fetchone():
                print("The initial digest has already been queued; use /digest for a new preview")
                return
            parts = await Digests(config, credentials, store, reader).build(
                store.last_window_end(),
                end,
                preview=True,
            )
            run_id = store.enqueue(
                slot, store.last_window_end(), end, store.get("mode"), parts, advance=False
            )
            # Pairing established the destination; queuing explicitly requests delivery.
            store.set("started", True)
            store.set("initial_digest_queued_at", stamp(utcnow()))
            print(f"Initial digest queued: run {run_id}, {len(parts)} parts")
    finally:
        await reader.client.disconnect()
        store.close()


def status(config: Config) -> None:
    import sqlite3

    uri = config.storage.database_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        settings = dict(connection.execute("SELECT key,value FROM meta"))
        result = {
            key: json.loads(settings[key]) if key in settings else None
            for key in (
                "folder_id",
                "folder_title",
                "language",
                "mode",
                "folder_error",
                "digest_error",
                "last_ai_input",
            )
        }
        result["sources"] = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled=1",
        ).fetchone()[0]
        result["messages"] = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        result["deliveries"] = dict(
            connection.execute(
                "SELECT status,COUNT(*) FROM outbox GROUP BY status",
            )
        )
        print(json.dumps(result, ensure_ascii=True))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Private Telegram folder digests")
    parser.add_argument("--config", type=Path, default=Path("config/ufifeed.toml"))
    parser.add_argument("--credentials", type=Path, default=Path("secrets/credentials.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("setup", "run", "check", "healthcheck", "init-key", "status"):
        commands.add_parser(command)
    auth_command = commands.add_parser("auth", help="Authorize the reading account")
    auth_command.add_argument(
        "--qr", action="store_true", help="Approve login from Telegram on phone"
    )
    configure_command = commands.add_parser(
        "configure", help="Configure a reader folder before startup"
    )
    configure_command.add_argument("--folder", required=True, help="Exact reader folder name")
    configure_command.add_argument("--language", choices=("en", "ru"))
    configure_command.add_argument("--mode", choices=("extractive", "ai"))
    configure_command.add_argument("--enable-ai-for-folder", action="store_true")
    configure_command.add_argument("--queue-digest", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Third-party network logs can contain credentials, input content or account metadata.
    for name in ("httpx", "httpcore", "telethon", "aiogram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "setup":
            setup(args.config, args.credentials)
            return
        config = Config.load(args.config)
        if args.command == "check":
            print("Configuration OK")
        elif args.command == "status":
            status(config)
        elif args.command == "init-key":
            if config.storage.database_path.exists():
                raise CLIError("Refusing to create a new key for an existing database")
            create_key(config.storage.key_path)
            print("Data encryption key created")
        elif args.command == "healthcheck":
            path = config.storage.database_path.parent / "heartbeat"
            age = time.time() - float(path.read_text())
            if not 0 <= age < 60:
                raise CLIError("Stale heartbeat")
        elif args.command == "backup":
            import sqlite3

            if args.destination.exists():
                raise CLIError("Backup destination already exists")
            with session_lock(config.telegram.session_path):
                store = Store.open(config)
                try:
                    with sqlite3.connect(args.destination) as target:
                        store.conn.backup(target)
                finally:
                    store.close()
            print("Encrypted database backed up; copy credentials/session/data.key separately")
        else:
            credentials = Credentials.load(args.credentials, bot_required=args.command != "auth")
            if args.command == "auth":
                asyncio.run(auth(config, credentials, use_qr=args.qr))
            elif args.command == "configure":
                with session_lock(config.telegram.session_path):
                    asyncio.run(configure(config, credentials, args))
            else:
                from ufifeed.app import run

                with session_lock(config.telegram.session_path):
                    asyncio.run(run(config, credentials))
    except KeyboardInterrupt:
        print("\nOperation cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        # Never dump exception bodies/tracebacks containing secrets to terminal or Docker logs.
        if isinstance(exc, (CLIError, AuthError, FileNotFoundError, FileExistsError)):
            safe = str(exc)
        elif isinstance(exc, ValidationError):
            safe = "Invalid fields: " + ", ".join(
                ".".join(str(part) for part in error["loc"]) for error in exc.errors()
            )
        else:
            safe = type(exc).__name__
        print(
            f"ufifeed: {safe}. Check configuration, credentials and setup instructions.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
