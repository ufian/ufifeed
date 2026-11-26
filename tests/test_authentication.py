import asyncio
import os
import pty
import select
import signal
import subprocess
import sys
import termios
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types

from ufifeed.authentication import AuthError, delivery_hint, login, prompt, qr_login


def client_stub():
    return SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(
            return_value=types.auth.SentCode(
                type=types.auth.SentCodeTypeApp(5),
                phone_code_hash="secret-hash",
                timeout=60,
            )
        ),
        sign_in=AsyncMock(),
    )


async def test_phone_login_explains_actual_delivery_and_never_prints_secrets(monkeypatch, capsys):
    client = client_stub()
    monkeypatch.setattr(
        "ufifeed.authentication.prompt",
        AsyncMock(
            side_effect=["+1 (202) 555-0101", "12345"],
        ),
    )
    await login(client, use_qr=False)
    output = capsys.readouterr().out
    assert "служебный чат Telegram" in output
    assert "auth --qr" in output
    assert "12345" not in output and "secret-hash" not in output
    assert "2025550101" not in output
    client.sign_in.assert_awaited_once_with(
        phone="+12025550101",
        code="12345",
        phone_code_hash="secret-hash",
    )


async def test_existing_session_does_not_request_another_code(monkeypatch):
    client = client_stub()
    client.is_user_authorized.return_value = True
    monkeypatch.setattr("ufifeed.authentication.prompt", AsyncMock(side_effect=AssertionError))
    await login(client, use_qr=False)
    client.send_code_request.assert_not_awaited()


async def test_two_factor_after_phone_code(monkeypatch):
    client = client_stub()
    client.sign_in.side_effect = [errors.SessionPasswordNeededError(None), None]
    monkeypatch.setattr(
        "ufifeed.authentication.prompt",
        AsyncMock(
            side_effect=["+12025550101", "12345", "private-password"],
        ),
    )
    await login(client, use_qr=False)
    assert client.sign_in.await_args.kwargs == {"password": "private-password"}


async def test_flood_wait_reports_delay_without_resending(monkeypatch):
    client = client_stub()
    client.send_code_request.side_effect = errors.FloodWaitError(None, capture=300)
    monkeypatch.setattr("ufifeed.authentication.prompt", AsyncMock(return_value="+12025550101"))
    with pytest.raises(AuthError, match="300 секунд"):
        await login(client, use_qr=False)
    assert client.send_code_request.await_count == 1


async def test_qr_wait_is_running_before_display_and_expired_qr_refreshes(monkeypatch):
    running = []

    async def wait_expired():
        running.append("expired")
        await asyncio.sleep(0)
        raise TimeoutError

    async def wait_success():
        running.append("success")
        await asyncio.sleep(0)

    client = SimpleNamespace(
        qr_login=AsyncMock(
            side_effect=[
                SimpleNamespace(url="example-expired", wait=wait_expired),
                SimpleNamespace(url="example-success", wait=wait_success),
            ]
        )
    )
    displayed = []

    def display(url):
        assert running
        displayed.append(url)

    monkeypatch.setattr("ufifeed.authentication.display_qr", display)
    await qr_login(client)
    assert displayed == ["example-expired", "example-success"]


async def test_qr_can_request_two_factor(monkeypatch):
    client = client_stub()
    client.qr_login = AsyncMock(
        return_value=SimpleNamespace(
            url="example",
            wait=AsyncMock(side_effect=errors.SessionPasswordNeededError(None)),
        )
    )
    monkeypatch.setattr("ufifeed.authentication.display_qr", lambda url: None)
    monkeypatch.setattr("ufifeed.authentication.prompt", AsyncMock(return_value="private-password"))
    await login(client, use_qr=True)
    client.sign_in.assert_awaited_once_with(password="private-password")


async def test_prompt_can_be_cancelled_and_reused_without_blocked_input_thread(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as stream:
        monkeypatch.setattr("ufifeed.authentication.sys.stdin", stream)
        task = asyncio.create_task(prompt("Input: "))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        next_prompt = asyncio.create_task(prompt("Again: "))
        os.write(write_fd, b"done\n")
        assert await asyncio.wait_for(next_prompt, 1) == "done"
    os.close(write_fd)


def test_unsupported_sms_route_explains_official_app_requirement():
    with pytest.raises(AuthError, match="официального приложения"):
        delivery_hint(SimpleNamespace(type=types.auth.SentCodeTypeSetUpEmailRequired()))


def test_ctrl_c_exits_real_terminal_prompt_and_restores_echo():
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    script = (
        "import asyncio\n"
        "from ufifeed.authentication import prompt\n"
        "try:\n"
        "    asyncio.run(prompt('Secret: ', secret=True))\n"
        "except KeyboardInterrupt:\n"
        "    print('cancelled', flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    try:
        assert select.select([master], [], [], 5)[0]
        assert b"Secret: " in os.read(master, 8192)
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 0
        assert termios.tcgetattr(slave)[3] == before[3]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
