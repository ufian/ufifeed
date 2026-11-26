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
from ufifeed.reader import ReaderError
from ufifeed.store import Store
from ufifeed.timeutil import next_slot, stamp, utcnow

log = logging.getLogger(__name__)

HELP = """ufifeed — сводки из папки отдельного Telegram-аккаунта.

/folders — папки читающего аккаунта
/folder ID — выбрать папку (0 — отключить сбор)
/sources — источники и состояние сбора
/topics слово, фраза — темы интереса; off — все темы
/exclude слово, фраза — исключения; off — очистить
/schedule 09:00,20:00 America/Los_Angeles — время сводок
/schedule — текущее расписание
/pause /resume — выключить/включить расписание
/digest — сводка сейчас, без изменения расписания
/mode extractive|ai — подборка или AI-сводка
/ai_source ID on|off — AI-доступ конкретного источника
/status — состояние и ошибки
/retry ID — повтор неопределённой/ошибочной доставки
/purge ID — удалить сохранённые данные источника

В папку добавляйте каналы и группы явно, без категорий «все чаты» и фильтров непрочитанного.
AI-доступ означает передачу текста настроенному AI-сервису.
Включайте его для источников, для которых установлено подходящее основание обработки.
Новые источники добавляются без AI-доступа.
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
            f"Расписание: {'включено' if schedule.enabled else 'выключено'}\n"
            f"{', '.join(schedule.times)} · {schedule.timezone}\n"
            f"Ближайшее время: {next_due.astimezone(ZoneInfo(schedule.timezone)):%d.%m %H:%M %Z}"
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
                else "Проверьте формат команды. /help"
            )
        except Exception as exc:
            log.warning("Control command failed: %s", type(exc).__name__)
            text = f"Команда не выполнена: {type(exc).__name__}. Повторите позже."
        await reply(message, text)

    async def execute(self, command: str, argument: str) -> str:
        if command in {"/start", "/help"}:
            self.store.set("started", True)
            return HELP
        if command == "/folders":
            folders = await self.reader.folders()
            return (
                (
                    "Папки читающего аккаунта:\n"
                    + "\n".join(f"{folder['id']} — {folder['title']}" for folder in folders)
                    + "\nВыбор: /folder ID"
                )
                if folders
                else "Папок нет. Создайте папку в читающем аккаунте."
            )
        if command == "/folder":
            folder_id = int(argument)
            if folder_id < 0:
                raise ValueError("Invalid folder")
            if folder_id and not any(f["id"] == folder_id for f in await self.reader.folders()):
                return "Папка не найдена. Посмотрите /folders."
            self.store.change_folder(folder_id or None)
            self.reader.wake.set()
            return (
                "Папка выбрана, синхронизация запрошена. /status" if folder_id else "Сбор отключён."
            )
        if command == "/sources":
            sources = self.store.sources()
            return (
                "\n".join(
                    f"{s['id']} — {s['title']} | AI: {'on' if s['ai_allowed'] else 'off'} | "
                    f"{s['error'] or ('синхронизирован' if s['initialized'] else 'ожидание')}"
                    for s in sources
                )
                or "Источников пока нет. Выберите папку: /folders"
            )
        if command in {"/topics", "/exclude"}:
            key = command[1:]
            if not argument:
                return f"{key}: {', '.join(self.store.get(key, [])) or 'нет'}"
            words = (
                [] if argument == "off" else [v.strip() for v in argument.split(",") if v.strip()]
            )
            if len(words) > 50 or any(len(word) > 100 for word in words):
                raise ValueError("Too many keywords")
            self.store.set(key, words)
            return "Настройки сохранены."
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
        if command == "/digest":
            if self.digests.lock.locked():
                return "Сводка уже готовится. Проверьте /status через минуту."
            run = await self.digests.manual()
            return f"Сводка #{run} подготовлена и поставлена в очередь доставки."
        if command == "/mode":
            if not argument:
                return f"Режим: {self.store.get('mode')}"
            if argument not in {"extractive", "ai"}:
                raise ValueError("Invalid mode")
            if argument == "ai" and not self.config.ai.enabled:
                return "AI-сервис ещё не настроен на сервере. Сейчас доступен extractive."
            self.store.set("mode", argument)
            return f"Режим: {argument}. AI обрабатывает только источники с AI: on в /sources."
        if command == "/ai_source":
            source, state = argument.split()
            source_id = int(source)
            if state not in {"on", "off"}:
                raise ValueError("Invalid state")
            if source_id not in {s["id"] for s in self.store.sources()}:
                return "Активный источник не найден. /sources"
            with self.store.conn:
                self.store.conn.execute(
                    "UPDATE sources SET ai_allowed=? WHERE id=?", (int(state == "on"), source_id)
                )
            if state == "off":
                self.store.invalidate_pending()
            return f"AI-доступ источника {source_id}: {state}."
        if command == "/status":
            return (
                f"Папка: {self.store.get('folder_title', 'не выбрана')} "
                f"(ID {self.store.get('folder_id')})\n"
                f"Источников: {len(self.store.sources())}\n"
                f"Пропущено личных диалогов/ботов: {self.store.get('folder_skipped', 0)}\n"
                f"Сверка папки: {self.store.get('folder_synced_at') or 'ещё не выполнена'}\n"
                f"Ошибка папки: {self.store.get('folder_error') or 'нет'}\n"
                f"Ошибка сводки: {self.store.get('digest_error') or 'нет'}\n"
                f"Режим: {self.store.get('mode')}\n"
                f"Очередь: {self.store.delivery_status()}\n"
                f"Проблемные доставки: {self.store.delivery_issues()}\n" + self.schedule_text()
            )
        if command == "/retry":
            parts = argument.split()
            if not parts:
                return "Используйте /retry ID. Номера проблемных доставок — в /status."
            item_id = int(parts[0])
            if len(parts) != 2 or parts[1] != "confirm":
                return (
                    f"При неизвестном результате повтор может создать дубль. "
                    f"Для повтора: /retry {item_id} confirm"
                )
            return (
                "Повтор поставлен в очередь."
                if self.store.retry(item_id)
                else "Доставка не найдена."
            )
        if command == "/purge":
            parts = argument.split()
            if not parts:
                return "Используйте /purge ID. Номера источников — в /sources."
            source_id = int(parts[0])
            if len(parts) != 2 or parts[1] != "confirm":
                return (
                    "Сначала уберите источник из папки, иначе он будет собран снова. "
                    f"Удалятся сообщения и сохранённые тексты сводок: /purge {source_id} confirm"
                )
            if source_id not in {s["id"] for s in self.store.sources(active_only=False)}:
                return "Источник не найден."
            if source_id in {s["id"] for s in self.store.sources()}:
                return "Источник ещё в папке. Сначала удалите его там и дождитесь синхронизации."
            self.store.purge(source_id)
            return "Сохранённые сообщения и тексты сводок удалены."
        return "Неизвестная команда. /help"


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
                ("digest", "Сводка сейчас"),
                ("status", "Состояние"),
                ("folders", "Выбрать папку"),
                ("sources", "Источники"),
                ("topics", "Темы"),
                ("schedule", "Расписание"),
                ("pause", "Приостановить сводки"),
                ("resume", "Возобновить сводки"),
                ("help", "Все команды"),
            ]
        ]
    )
