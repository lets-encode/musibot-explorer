"""Shared bits for the two exploration scripts: server address, token, output folder."""

import os
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "https://quest.ms.mff.cuni.cz/musibot/api"
OUT_DIR = Path(__file__).parent / "out"
ENV_FILE = Path(__file__).parent / ".env"

# KEY=value lines from .env (gitignored) fill in what the shell did not set.
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.strip().strip('"'))


def api_url() -> str:
    return os.environ.get("MUSIBOT_URL", DEFAULT_URL).rstrip("/")


def token(url: str) -> str:
    """The token from MUSIBOT_TOKEN, or a freshly minted public session.

    A public session is capped by the server: one execution at a time, five
    pages, a 60 s execution timeout, and it expires after an hour with its pages.
    """
    given = os.environ.get("MUSIBOT_TOKEN")
    if given:
        return given
    response = httpx.post(f"{url}/public-sessions", timeout=30)
    response.raise_for_status()
    session = response.json()
    print(
        f"[public session] no MUSIBOT_TOKEN set; minted a public session "
        f"(expires {session['expires_at']}, 1 execution at a time, 60 s timeout)",
        file=sys.stderr,
    )
    return session["token"]


def out_dir(page_id: str) -> Path:
    folder = OUT_DIR / page_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save(folder: Path, path: str, content: bytes) -> Path:
    target = folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target
