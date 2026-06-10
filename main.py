from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from tqdm import tqdm

from config import DEFAULT_INPUT_FILE, DEFAULT_OUTPUT_FILE, ensure_directories, load_settings
from scraper import ProjectListing, WebsiteScraper
from telegram_client import TelegramAdminExtractor, TelegramGroupInfo
from utils import dedupe_preserve_order, save_rows_csv, setup_logging


logger = logging.getLogger(__name__)


def read_urls(input_file: Path) -> list[str]:
    if not input_file.exists():
        raise FileNotFoundError(f"Input URL file not found: {input_file}")

    urls = []
    for line in input_file.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            urls.append(value)
    return dedupe_preserve_order(urls)


def flatten_telegram_links(projects: list[ProjectListing]) -> list[str]:
    links: list[str] = []
    for project in projects:
        links.extend(project.telegram_links)
    return dedupe_preserve_order(links)


def build_csv_rows(projects: list[ProjectListing], group_map: dict[str, TelegramGroupInfo]) -> list[dict]:
    rows: list[dict] = []
    for project in projects:
        for telegram_link in project.telegram_links:
            group_info = group_map.get(telegram_link) or TelegramGroupInfo(telegram_link=telegram_link)
            admins = group_info.admins
            base_row = {
                "project_name": project.project_name,
                "project_url": project.project_url,
                "date_posted": project.date_posted.isoformat() if project.date_posted else "",
                "telegram_link": telegram_link,
                "channel_title": group_info.channel_title,
                "channel_username": group_info.channel_username,
                "channel_description": group_info.channel_description,
                "linked_contacts": " | ".join(group_info.linked_contacts),
                "pinned_messages": " | ".join(group_info.pinned_messages),
            }

            if not admins:
                rows.append(
                    {
                        **base_row,
                        "admin_name": "",
                        "admin_username": "",
                        "telegram_user_id": "",
                        "role": "unknown",
                    }
                )
                continue

            for admin in admins:
                rows.append(
                    {
                        **base_row,
                        "admin_name": admin.admin_name,
                        "admin_username": admin.admin_username,
                        "telegram_user_id": admin.telegram_user_id or "",
                        "role": admin.role,
                    }
                )
    return rows


async def run(input_file: Path, output_file: Path) -> None:
    setup_logging()
    ensure_directories()
    settings = load_settings()

    urls = read_urls(input_file)
    if not urls:
        logger.warning("No URLs found in %s", input_file)
        save_rows_csv([], str(output_file))
        return

    scraper = WebsiteScraper(settings)
    projects = await scraper.scrape_sites(urls)
    telegram_links = flatten_telegram_links(projects)
    logger.info("Found %s unique Telegram links", len(telegram_links))

    group_map: dict[str, TelegramGroupInfo] = {}
    if telegram_links:
        async with TelegramAdminExtractor(settings) as extractor:
            for link in tqdm(telegram_links, desc="Telegram groups"):
                group_map[link] = await extractor.extract_group_info(link)

    rows = build_csv_rows(projects, group_map)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    save_rows_csv(rows, str(output_file))
    logger.info("Saved %s rows to %s", len(rows), output_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape recent projects and public Telegram admins.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_FILE, help="Text file containing website URLs.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE, help="CSV output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args.input, args.output))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception as exc:
        logger.exception("Fatal startup error: %s", exc)


if __name__ == "__main__":
    main()
