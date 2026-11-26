from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import termios

import qrcode
from telethon import errors


class AuthError(Exception):
    """Safe authentication diagnostics, without credentials or server response bodies."""


async def prompt(text: str, *, secret: bool = False) -> str:
    """POSIX terminal input that stays cancellable on Ctrl+C while asyncio is running."""
    loop = asyncio.get_running_loop()
    stream = sys.stdin
    fd = stream.fileno()
    previous = None
    if secret and stream.isatty():
        previous = termios.tcgetattr(fd)
        hidden = previous.copy()
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
    result = loop.create_future()

    def ready() -> None:
        if result.done():
            return
        try:
            line = stream.readline()
            if not line:
                raise EOFError
            result.set_result(line.rstrip("\r\n"))
        except Exception as exc:
            result.set_exception(exc)

    try:
        print(text, end="", flush=True)
        loop.add_reader(fd, ready)
        return await result
    finally:
        loop.remove_reader(fd)
        if previous is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
            print(flush=True)


def delivery_hint(sent) -> str:
    if not hasattr(sent, "type"):
        raise AuthError(
            "Telegram не вернул обычный способ доставки кода. "
            "Попробуйте auth --qr из уже вошедшего официального приложения."
        )
    kind = type(sent.type).__name__
    messages = {
        "SentCodeTypeApp": "Код отправлен в служебный чат Telegram на уже вошедших устройствах "
        "читающего аккаунта. Это не SMS и не сообщение нашему боту.",
        "SentCodeTypeSms": "Telegram выбрал доставку кода по SMS.",
        "SentCodeTypeCall": "Telegram выбрал телефонный звонок с кодом.",
        "SentCodeTypeFlashCall": "Telegram выбрал проверку телефонным звонком.",
        "SentCodeTypeMissedCall": "Telegram выбрал проверку пропущенным звонком.",
        "SentCodeTypeEmailCode": "Код отправлен на email для входа в Telegram.",
        "SentCodeTypeFragmentSms": "Telegram выбрал Fragment. Используйте доступный вам "
        "Fragment или выполните auth --qr с уже вошедшего телефона.",
    }
    if kind in {"SentCodeTypeFirebaseSms", "SentCodeTypeSetUpEmailRequired"}:
        raise AuthError(
            "Этот способ входа требует официального приложения Telegram. "
            "Сначала войдите в читающий аккаунт на телефоне, затем используйте auth --qr."
        )
    return messages.get(
        kind, f"Telegram выбрал способ {kind}; если код не приходит, используйте auth --qr."
    )


async def password_login(client) -> None:
    for _ in range(3):
        password = await prompt(
            "Пароль двухэтапной аутентификации читающего аккаунта: ", secret=True
        )
        try:
            await asyncio.wait_for(client.sign_in(password=password), timeout=45)
            return
        except errors.PasswordHashInvalidError:
            print("Пароль не подошёл. Попробуйте ещё раз.", flush=True)
    raise AuthError("Три неверных пароля. Вход остановлен.")


async def code_login(client) -> None:
    phone = await prompt("Номер ЧИТАЮЩЕГО аккаунта в международном формате (+код страны…): ")
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        raise AuthError("Нужен номер с + и кодом страны. Bot token здесь не используется.")
    print("Запрашиваю код у Telegram…", flush=True)
    sent = await asyncio.wait_for(client.send_code_request(phone), timeout=45)
    print(delivery_hint(sent), flush=True)
    if getattr(sent, "timeout", None):
        print(
            f"Telegram указал таймаут {sent.timeout} секунд; "
            "повтор автоматически не запрашивается.",
            flush=True,
        )
    print(
        "Если кода нет: Ctrl+C, затем docker compose run --rm app auth --qr. "
        "Не запускайте много запросов подряд.",
        flush=True,
    )
    for _ in range(3):
        code = await prompt("Код (ввод скрыт): ", secret=True)
        try:
            await asyncio.wait_for(
                client.sign_in(
                    phone=phone,
                    code=code.strip(),
                    phone_code_hash=sent.phone_code_hash,
                ),
                timeout=45,
            )
            return
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            print("Код не подошёл. Проверьте последний код для читающего аккаунта.", flush=True)
        except errors.PhoneCodeExpiredError:
            raise AuthError(
                "Код истёк. Запустите auth заново либо используйте auth --qr."
            ) from None
    raise AuthError("Три неверных кода. Вход остановлен.")


def display_qr(url: str) -> None:
    qr = qrcode.QRCode(border=3)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)
    sys.stdout.flush()


async def qr_login(client) -> None:
    print(
        "На телефоне переключитесь в ЧИТАЮЩИЙ аккаунт Telegram.\n"
        "Настройки → Устройства → Подключить устройство.\n"
        "Сканируйте QR с экрана терминала и подтвердите вход. "
        "Не публикуйте этот QR. Ctrl+C отменяет вход.",
        flush=True,
    )
    for _ in range(10):
        login = await asyncio.wait_for(client.qr_login(), timeout=45)
        waiting = asyncio.create_task(login.wait())
        try:
            # Telethon's update handler must be installed before the QR can be scanned.
            await asyncio.sleep(0)
            display_qr(login.url)
            await waiting
            return
        except TimeoutError:
            print("QR истёк. Ниже новый код:", flush=True)
        finally:
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiting
    raise AuthError("Время входа по QR истекло. Повторите auth --qr.")


async def login(client, *, use_qr: bool) -> None:
    try:
        print("Подключаюсь к Telegram…", flush=True)
        await asyncio.wait_for(client.connect(), timeout=30)
        if await asyncio.wait_for(client.is_user_authorized(), timeout=30):
            print("Сессия уже авторизована.", flush=True)
            return
        try:
            if use_qr:
                await qr_login(client)
            else:
                await code_login(client)
        except errors.SessionPasswordNeededError:
            await password_login(client)
    except errors.FloodWaitError as exc:
        raise AuthError(
            f"Telegram ограничил запросы. Подождите минимум {exc.seconds} секунд. "
            "Повторные запросы сейчас не помогут."
        ) from None
    except TimeoutError:
        raise AuthError(
            "Telegram не ответил вовремя. Проверьте соединение и повторите позже."
        ) from None
    except EOFError:
        raise AuthError(
            "Ввод закрыт. Запустите auth в интерактивном терминале через ssh -t."
        ) from None
    except errors.ApiIdInvalidError:
        raise AuthError("Telegram отклонил api_id/api_hash. Проверьте credentials на VM.") from None
    except errors.PhoneNumberInvalidError:
        raise AuthError("Telegram отклонил номер. Проверьте международный формат.") from None
    except errors.PhoneNumberBannedError:
        raise AuthError("Telegram сообщает об ограничении этого номера.") from None
