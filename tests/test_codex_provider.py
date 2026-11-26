import asyncio
import json
import os
import sys

import pytest

from ufifeed.codex_provider import CodexError, complete
from ufifeed.config import AI


def executable(tmp_path, body):
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return AI(enabled=True, provider="codex", codex_path=str(path))


async def test_codex_content_uses_stdin_and_result_is_structured(tmp_path):
    settings = executable(
        tmp_path,
        """import json, pathlib, sys
assert "private source" not in " ".join(sys.argv)
assert "private source" in sys.stdin.read()
assert sys.argv[sys.argv.index("--sandbox") + 1] == "read-only"
assert "--ignore-user-config" in sys.argv and "--ephemeral" in sys.argv
target = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
target.write_text('{"items":[{"text":"Summary","refs":[1]}]}')
print(json.dumps({"type":"turn.completed"}))
""",
    )
    result = await complete(settings, "Summarize in English", [{"text": "private source"}], {})
    assert json.loads(result)["items"][0]["refs"] == [1]


async def test_unexpected_codex_tool_activity_is_rejected(tmp_path):
    settings = executable(
        tmp_path,
        """import json, sys
sys.stdin.read()
print(json.dumps({"item":{"type":"command_execution"}}))
""",
    )
    with pytest.raises(CodexError, match="tool activity"):
        await complete(settings, "Instructions", [], {})


async def test_codex_timeout_kills_the_worker(tmp_path):
    pidfile = tmp_path / "pid"
    settings = executable(
        tmp_path,
        f"""import os, pathlib, time
pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
time.sleep(30)
""",
    ).model_copy(update={"timeout_seconds": 0.3})
    with pytest.raises(CodexError, match="timed out"):
        await complete(settings, "Instructions", [], {})
    if pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


async def test_cancellation_kills_codex_without_leaving_a_background_job(tmp_path):
    pidfile = tmp_path / "pid"
    settings = executable(
        tmp_path,
        f"""import os, pathlib, time
pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
time.sleep(30)
""",
    )
    task = asyncio.create_task(complete(settings, "Instructions", [], {}))
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
