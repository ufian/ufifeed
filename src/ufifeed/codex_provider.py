from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from tempfile import TemporaryDirectory

from ufifeed.config import AI

DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "apps",
    "plugins",
    "hooks",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "multi_agent",
    "code_mode",
    "code_mode_host",
    "workspace_dependencies",
    "skill_search",
    "tool_suggest",
    "memories",
)


class CodexError(Exception):
    pass


async def complete(settings: AI, instructions: str, records: list[dict], schema: dict) -> str:
    """Use the authenticated CLI as a text-only, ephemeral structured-output worker."""
    with TemporaryDirectory(prefix="ufifeed-summary-") as temporary:
        work = Path(temporary)
        schema_path, output_path = work / "schema.json", work / "result.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            settings.codex_path,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(work),
            "-c",
            'web_search="disabled"',
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if settings.model:
            command += ["--model", settings.model]
        for feature in DISABLED_FEATURES:
            command += ["--disable", feature]
        command.append("-")
        # Pass content only over stdin, never process arguments or command logs.
        payload = (
            instructions + "\n\nINPUT_MESSAGES_JSON:\n" + json.dumps(records, ensure_ascii=False)
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode()),
                timeout=settings.timeout_seconds,
            )
        except TimeoutError:
            raise CodexError("Codex summary timed out") from None
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode:
            error = stderr.decode(errors="replace").casefold()
            if any(word in error for word in ("usage_limit", "rate_limit", "quota")):
                raise CodexError("Codex account usage limit reached")
            raise CodexError(
                f"Codex exited with status {process.returncode}; check CLI login status"
            )
        if len(stdout) > 2_000_000:
            raise CodexError("Codex output exceeded the size limit")
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                raise CodexError("Invalid Codex event stream") from None
            item = event.get("item", {})
            if item.get("type") in {
                "command_execution",
                "mcp_tool_call",
                "web_search",
                "image_generation",
            }:
                raise CodexError("Unexpected tool activity in text-only summary mode")
        if not output_path.is_file() or output_path.stat().st_size > 100_000:
            raise CodexError("Codex did not produce a bounded structured summary")
        return output_path.read_text(encoding="utf-8")
