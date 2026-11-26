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
            "Telegram did not return a regular code delivery method. "
            "Try auth --qr from an official app that is already signed in."
        )
    kind = type(sent.type).__name__
    messages = {
        "SentCodeTypeApp": "The code was sent to the Telegram service chat on devices signed into "
        "the reader account. This is not an SMS or a message to our bot.",
        "SentCodeTypeSms": "Telegram selected SMS code delivery.",
        "SentCodeTypeCall": "Telegram selected a phone call with the code.",
        "SentCodeTypeFlashCall": "Telegram selected flash-call verification.",
        "SentCodeTypeMissedCall": "Telegram selected missed-call verification.",
        "SentCodeTypeEmailCode": "The code was sent to the login email configured in Telegram.",
        "SentCodeTypeFragmentSms": "Telegram selected Fragment. Use your available "
        "Fragment account or run auth --qr from a phone already signed in.",
    }
    if kind in {"SentCodeTypeFirebaseSms", "SentCodeTypeSetUpEmailRequired"}:
        raise AuthError(
            "This login method requires an official Telegram app. "
            "Sign into the reader account on your phone, then use auth --qr."
        )
    return messages.get(
        kind, f"Telegram selected {kind}; if the code does not arrive, use auth --qr."
    )


async def password_login(client) -> None:
    for _ in range(3):
        password = await prompt(
            "Two-step verification password for the reader account: ", secret=True
        )
        try:
            await asyncio.wait_for(client.sign_in(password=password), timeout=45)
            return
        except errors.PasswordHashInvalidError:
            print("Incorrect password. Try again.", flush=True)
    raise AuthError("Three incorrect passwords. Login stopped.")


async def code_login(client) -> None:
    phone = await prompt("READER account phone number in international format (+country code...): ")
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        raise AuthError(
            "Enter a phone number starting with + and a country code. Do not enter a bot token."
        )
    print("Requesting a code from Telegram...", flush=True)
    sent = await asyncio.wait_for(client.send_code_request(phone), timeout=45)
    print(delivery_hint(sent), flush=True)
    if getattr(sent, "timeout", None):
        print(
            f"Telegram returned a {sent.timeout}-second timeout; "
            "a new code will not be requested automatically.",
            flush=True,
        )
    print(
        "If no code arrives: Ctrl+C, then run /home/ufian/ufifeed/compose run --rm app auth --qr. "
        "Avoid repeated code requests.",
        flush=True,
    )
    for _ in range(3):
        code = await prompt("Code (input hidden): ", secret=True)
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
            print("Incorrect code. Check the latest code for the reader account.", flush=True)
        except errors.PhoneCodeExpiredError:
            raise AuthError("The code expired. Restart auth or use auth --qr.") from None
    raise AuthError("Three incorrect codes. Login stopped.")


def display_qr(url: str) -> None:
    qr = qrcode.QRCode(border=3)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)
    sys.stdout.flush()


async def qr_login(client) -> None:
    print(
        "Switch to the READER Telegram account on your phone.\n"
        "Settings -> Devices -> Link Desktop Device.\n"
        "Scan the QR code from this terminal and approve the login. "
        "Do not share this QR code. Ctrl+C cancels login.",
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
            print("The QR code expired. A new code follows:", flush=True)
        finally:
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiting
    raise AuthError("QR login timed out. Run auth --qr again.")


async def login(client, *, use_qr: bool) -> None:
    try:
        print("Connecting to Telegram...", flush=True)
        await asyncio.wait_for(client.connect(), timeout=30)
        if await asyncio.wait_for(client.is_user_authorized(), timeout=30):
            print("The session is already authorized.", flush=True)
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
            f"Telegram rate-limited this request. Wait at least {exc.seconds} seconds. "
            "Repeated requests will not help right now."
        ) from None
    except TimeoutError:
        raise AuthError(
            "Telegram did not respond in time. Check the connection and try later."
        ) from None
    except EOFError:
        raise AuthError(
            "Input is closed. Run auth in an interactive terminal using ssh -t."
        ) from None
    except errors.ApiIdInvalidError:
        raise AuthError("Telegram rejected api_id/api_hash. Check credentials on the VM.") from None
    except errors.PhoneNumberInvalidError:
        raise AuthError(
            "Telegram rejected the phone number. Check its international format."
        ) from None
    except errors.PhoneNumberBannedError:
        raise AuthError("Telegram reports that this phone number is banned.") from None
