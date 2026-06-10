from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "session"
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_INPUT_FILE = BASE_DIR / "urls.txt"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "output.csv"


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    phone: str
    proxy_host: str | None
    proxy_port: int | None
    proxy_user: str | None
    proxy_pass: str | None
    session_name: str
    request_timeout_seconds: int
    max_pages_per_site: int
    concurrency: int


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    phone = os.getenv("PHONE", "").strip()

    missing = [name for name, value in {
        "API_ID": api_id,
        "API_HASH": api_hash,
        "PHONE": phone,
    }.items() if not value]
    if missing:
        raise ValueError(f"Missing required .env values: {', '.join(missing)}")

    return Settings(
        api_id=int(api_id),
        api_hash=api_hash,
        phone=phone,
        proxy_host=os.getenv("PROXY_HOST") or None,
        proxy_port=_optional_int(os.getenv("PROXY_PORT")),
        proxy_user=os.getenv("PROXY_USER") or None,
        proxy_pass=os.getenv("PROXY_PASS") or None,
        session_name=os.getenv("SESSION_NAME", "telegram_scrapper"),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        max_pages_per_site=int(os.getenv("MAX_PAGES_PER_SITE", "25")),
        concurrency=int(os.getenv("CONCURRENCY", "5")),
    )


def ensure_directories() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
