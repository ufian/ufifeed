"""Install the pinned official Linux x86_64 Codex binary after SHA-256 verification."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "0.172.0"
ASSET = "codex-x86_64-unknown-linux-musl.tar.gz"
SHA256 = "a2342a8f7f68f088be30620ccfe9cb84162c93a6b56177855961cda3ed6be392"
URL = f"https://github.com/openai/codex/releases/download/rust-v{VERSION}/{ASSET}"


def install(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ufifeed-codex-") as temporary:
        archive = Path(temporary) / ASSET
        with urllib.request.urlopen(URL, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as downloaded:
            digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
        if digest != SHA256:
            raise ValueError("Official Codex archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as package:
            matches = [
                member
                for member in package.getmembers()
                if member.isfile() and Path(member.name).name == ASSET.removesuffix(".tar.gz")
            ]
            if len(matches) != 1:
                raise ValueError("Expected exactly one Codex executable")
            # Read one known file; do not extract archive paths onto the filesystem.
            source = package.extractfile(matches[0])
            if source is None:
                raise ValueError("Could not read Codex executable")
            staged = destination.with_suffix(".installing")
            try:
                with source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(staged, 0o755)
                staged.replace(destination)
            finally:
                staged.unlink(missing_ok=True)
    print(f"Installed Codex {VERSION}: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    install(parser.parse_args().destination)
