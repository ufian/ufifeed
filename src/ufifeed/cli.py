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
from ufifeed.store import Store, create_key


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
        print(f"В основном Telegram-аккаунте откройте https://t.me/{user.username}")
        print(f"Отправьте ему: /start {token}", flush=True)
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
    print("Введите credentials непосредственно в этом терминале. Секреты не выводятся.")
    api_id = int(input("Telegram API ID читающего приложения: "))
    api_hash = getpass.getpass("Telegram API hash: ")
    bot_token = getpass.getpass("Токен нового бота от BotFather: ")
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
    print("Владелец привязан. Теперь выполните auth, затем запустите app и отправьте /start боту.")


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
            print("Читающий аккаунт авторизован. Сессия сохранена на VM.")
        finally:
            await client.disconnect()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Private Telegram folder digests")
    parser.add_argument("--config", type=Path, default=Path("config/ufifeed.toml"))
    parser.add_argument("--credentials", type=Path, default=Path("secrets/credentials.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("setup", "run", "check", "healthcheck", "init-key"):
        commands.add_parser(command)
    auth_command = commands.add_parser("auth", help="Authorize the reading account")
    auth_command.add_argument(
        "--qr", action="store_true", help="Approve login from Telegram on phone"
    )
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
            credentials = Credentials.load(args.credentials, bot_required=args.command == "run")
            if args.command == "auth":
                asyncio.run(auth(config, credentials, use_qr=args.qr))
            else:
                from ufifeed.app import run

                with session_lock(config.telegram.session_path):
                    asyncio.run(run(config, credentials))
    except KeyboardInterrupt:
        print("\nВход/операция отменены.", file=sys.stderr)
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
