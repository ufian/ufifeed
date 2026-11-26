from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Owner(StrictModel):
    user_id: int = Field(gt=0)


class Storage(StrictModel):
    database_path: Path = Path("data/ufifeed.sqlite3")
    key_path: Path = Path("secrets/data.key")
    retention_days: int = Field(default=0, ge=0, le=365)


class Telegram(StrictModel):
    session_path: Path = Path("secrets/reader.session")
    initial_history_hours: int = Field(default=24, ge=1, le=168)
    reconcile_interval_seconds: int = Field(default=300, ge=30)
    folder_id: int | None = Field(default=None, gt=0)


class Digest(StrictModel):
    max_items: int = Field(default=10, ge=1, le=30)
    send_when_empty: bool = False


class Schedule(StrictModel):
    enabled: bool = False
    timezone: str = "America/Los_Angeles"
    times: list[str] = Field(default_factory=lambda: ["09:00", "20:00"])

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("times")
    @classmethod
    def valid_times(cls, values: list[str]) -> list[str]:
        import re

        if not 1 <= len(values) <= 12:
            raise ValueError("Use between 1 and 12 daily times")
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) for v in values):
            raise ValueError("Time must be HH:MM")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate times")
        return sorted(values)


class AI(StrictModel):
    enabled: bool = False
    provider: Literal["chat-completions", "codex"] = "chat-completions"
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    codex_path: str = "/usr/local/bin/codex"
    timeout_seconds: int = Field(default=180, ge=30, le=600)
    daily_call_limit: int = Field(default=100, ge=1, le=1000)
    max_input_chars: int = Field(default=24000, ge=1000, le=100000)

    @model_validator(mode="after")
    def configured(self) -> AI:
        if self.enabled and self.provider == "chat-completions" and not self.model.strip():
            raise ValueError("Set ai.model before enabling AI")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("AI base_url must be an HTTP URL")
        return self


class MiniApp(StrictModel):
    enabled: bool = False
    public_url: str = ""
    port: int = Field(default=8082, ge=1024, le=65535)

    @model_validator(mode="after")
    def configured(self) -> MiniApp:
        from urllib.parse import urlsplit

        if self.enabled:
            url = urlsplit(self.public_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("Mini App requires an HTTPS origin URL")
            _ = url.port
        return self


class Config(StrictModel):
    owner: Owner
    storage: Storage = Field(default_factory=Storage)
    telegram: Telegram = Field(default_factory=Telegram)
    digest: Digest = Field(default_factory=Digest)
    schedule: Schedule = Field(default_factory=Schedule)
    ai: AI = Field(default_factory=AI)
    miniapp: MiniApp = Field(default_factory=MiniApp)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        with Path(path).open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))


class Credentials(BaseModel):
    api_id: int
    api_hash: SecretStr
    bot_token: SecretStr = SecretStr("")
    ai_api_key: SecretStr = SecretStr("")

    @classmethod
    def load(
        cls, path: Path = Path("secrets/credentials.json"), *, bot_required: bool = True
    ) -> Credentials:
        saved = json.loads(path.read_text()) if path.exists() else {}
        data = {
            "api_id": os.environ.get("TELEGRAM_API_ID") or saved.get("api_id", ""),
            "api_hash": os.environ.get("TELEGRAM_API_HASH") or saved.get("api_hash", ""),
            "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN") or saved.get("bot_token", ""),
            "ai_api_key": os.environ.get("AI_API_KEY") or saved.get("ai_api_key", ""),
        }
        if not data["api_id"] or not data["api_hash"] or (bot_required and not data["bot_token"]):
            raise ValueError("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN")
        return cls.model_validate(data)
