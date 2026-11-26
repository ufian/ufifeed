from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from aiogram import Bot

from ufifeed.bot import Controls, delivery_loop, set_commands
from ufifeed.config import Config, Credentials
from ufifeed.digest import DigestError, Digests
from ufifeed.reader import Reader
from ufifeed.store import Store

log = logging.getLogger(__name__)


async def scheduler(digests: Digests, store: Store) -> None:
    failures = 0
    while True:
        delay = 15
        try:
            await digests.scheduled()
            failures = 0
        except DigestError as exc:
            store.set("digest_error", str(exc))
            failures += 1
            delay = min(1800, 60 * 2 ** min(failures - 1, 5))
        except Exception as exc:
            log.warning("Digest preparation failed: %s", type(exc).__name__)
            store.set("digest_error", type(exc).__name__)
            failures += 1
            delay = min(1800, 60 * 2 ** min(failures - 1, 5))
        await asyncio.sleep(delay)


async def housekeeping(store: Store, retention_days: int) -> None:
    while True:
        store.cleanup(retention_days)
        await asyncio.sleep(3600)


async def heartbeat(path: Path, store: Store) -> None:
    import time

    while True:
        store.conn.execute("SELECT 1").fetchone()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(str(time.time()))
        temporary.replace(path)
        await asyncio.sleep(10)


async def run(config: Config, credentials: Credentials) -> None:
    store = Store.open(config)
    store.bootstrap(config)
    store.recover_sending()
    reader = Reader(config, credentials, store)
    bot = Bot(credentials.bot_token.get_secret_value())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    tasks = []
    try:
        # Polling remains available when the Telegram reading connection is temporarily down.
        await reader.connect()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError("Bot already has a webhook; use a dedicated bot without a webhook")
        await set_commands(bot)
        digests = Digests(config, credentials, store, reader)
        controls = Controls(config, store, reader, digests)
        tasks = [
            asyncio.create_task(reader.run(), name="collector"),
            asyncio.create_task(scheduler(digests, store), name="scheduler"),
            asyncio.create_task(
                housekeeping(store, config.storage.retention_days), name="housekeeping"
            ),
            asyncio.create_task(delivery_loop(bot, store, config.owner.user_id), name="delivery"),
            asyncio.create_task(
                heartbeat(config.storage.database_path.parent / "heartbeat", store),
                name="heartbeat",
            ),
            asyncio.create_task(
                controls.dispatcher.start_polling(
                    bot,
                    handle_signals=False,
                    close_bot_session=False,
                    allowed_updates=["message"],
                ),
                name="bot",
            ),
            asyncio.create_task(stop.wait(), name="shutdown"),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.get_name() != "shutdown":
                # Fail fast on a dead critical task. Docker can restart the process.
                if task.exception():
                    raise task.exception()
                raise RuntimeError(f"Critical task exited: {task.get_name()}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await reader.client.disconnect()
        await bot.session.close()
        store.close()
