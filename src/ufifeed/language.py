from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

LANGUAGES = {"en": "English", "ru": "Russian"}


def language_code(value: str) -> str:
    code = value.strip().lower()
    if code not in LANGUAGES:
        raise ValueError("Unsupported digest language. Use /language en or /language ru.")
    return code


@lru_cache(maxsize=2)
def digest_labels(code: str) -> dict[str, str]:
    resource = files("ufifeed.locales").joinpath(language_code(code) + ".json")
    return json.loads(resource.read_text(encoding="utf-8"))
