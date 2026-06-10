from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar
from urllib.parse import urlparse

import pandas as pd


T = TypeVar("T")
TELEGRAM_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[A-Za-z0-9_+/.-]+",
    re.IGNORECASE,
)
TELEGRAM_USERNAME_RE = re.compile(r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{4,31}\b")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def extract_telegram_links(text: str) -> list[str]:
    links: list[str] = []
    for match in TELEGRAM_RE.findall(text or ""):
        normalized = normalize_telegram_link(match)
        if normalized:
            links.append(normalized)
    return list(dict.fromkeys(links))


def normalize_telegram_link(link: str) -> str | None:
    link = link.strip().rstrip(").,;\"'")
    if not link:
        return None

    if not link.startswith(("http://", "https://")):
        link = "https://" + link

    parsed = urlparse(link)
    if parsed.netloc.lower().startswith("www."):
        netloc = parsed.netloc[4:]
    else:
        netloc = parsed.netloc

    if netloc.lower() not in {"t.me", "telegram.me", "telegram.dog"}:
        return None

    ignored_prefixes = ("/share/", "/joinchat/")
    if parsed.path.lower().startswith(ignored_prefixes):
        return None

    return f"https://t.me{parsed.path}".rstrip("/")


def dedupe_preserve_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def extract_public_contacts(text: str | None) -> list[str]:
    """Extract public Telegram contact handles and links from visible text."""
    if not text:
        return []

    contacts = extract_telegram_links(text)
    contacts.extend(TELEGRAM_USERNAME_RE.findall(text))
    return dedupe_preserve_order(contacts)


def cutoff_datetime(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def is_recent(value: datetime | None, days: int) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value >= cutoff_datetime(days)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None

    raw = value.strip()
    lowered = raw.lower()
    now = datetime.now(timezone.utc)

    relative = re.search(r"(\d+)\s+(minute|hour|day|week)s?\s+ago", lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit == "minute":
            return now - timedelta(minutes=amount)
        if unit == "hour":
            return now - timedelta(hours=amount)
        if unit == "day":
            return now - timedelta(days=amount)
        if unit == "week":
            return now - timedelta(weeks=amount)

    if lowered in {"today", "just now"}:
        return now
    if lowered == "yesterday":
        return now - timedelta(days=1)

    formats = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue

    try:
        parsed_date = date.fromisoformat(raw[:10])
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    except ValueError:
        return None


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    logger: logging.Logger | None = None,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_exceptions as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            if logger:
                logger.warning("Attempt %s/%s failed: %s. Retrying in %.1fs", attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise last_error or RuntimeError("retry_async failed without an exception")


def save_rows_csv(rows: list[dict], output_file: str) -> None:
    columns = [
        "project_name",
        "project_url",
        "date_posted",
        "telegram_link",
        "admin_name",
        "admin_username",
        "telegram_user_id",
        "role",
        "channel_title",
        "channel_username",
        "channel_description",
        "linked_contacts",
        "pinned_messages",
    ]
    df = pd.DataFrame(rows, columns=columns)
    df.to_csv(output_file, index=False, encoding="utf-8")
