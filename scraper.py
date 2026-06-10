from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests import Session

from config import Settings
from utils import extract_telegram_links, is_recent, parse_date, retry_async


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectListing:
    project_name: str
    project_url: str
    date_posted: datetime
    telegram_links: tuple[str, ...]


class WebsiteScraper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session: Session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    async def scrape_sites(self, urls: list[str]) -> list[ProjectListing]:
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def scrape_one(url: str) -> list[ProjectListing]:
            async with semaphore:
                try:
                    return await self.scrape_site(url)
                except Exception as exc:
                    logger.exception("Failed to scrape %s: %s", url, exc)
                    return []

        batches = await asyncio.gather(*(scrape_one(url) for url in urls))
        listings = [listing for batch in batches for listing in batch]
        logger.info("Collected %s recent project listings", len(listings))
        return listings

    async def scrape_site(self, start_url: str) -> list[ProjectListing]:
        found: list[ProjectListing] = []
        visited: set[str] = set()
        next_url: str | None = start_url
        page_count = 0

        while next_url and next_url not in visited and page_count < self.settings.max_pages_per_site:
            visited.add(next_url)
            page_count += 1
            logger.info("Scraping %s page %s", start_url, page_count)

            html = await self._fetch(next_url)
            soup = BeautifulSoup(html, "html.parser")
            page_listings = await self._extract_listings(soup, next_url)
            recent = [item for item in page_listings if is_recent(item.date_posted, self.settings.recent_days)]
            found.extend(recent)

            if page_listings and not recent:
                logger.info("Stopping pagination for %s because this page has no recent projects", start_url)
                break

            next_url = self._find_next_page(soup, next_url)

        return found

    async def _fetch(self, url: str) -> str:
        def request_page() -> str:
            response = self.session.get(url, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            return response.text

        return await retry_async(
            lambda: asyncio.to_thread(request_page),
            attempts=3,
            retry_exceptions=(requests.RequestException, TimeoutError, ConnectionError),
            logger=logger,
        )

    async def _extract_listings(self, soup: BeautifulSoup, page_url: str) -> list[ProjectListing]:
        candidates = self._listing_candidates(soup)
        listings: list[ProjectListing] = []

        for node in candidates:
            listing = await self._listing_from_node(node, page_url)
            if listing:
                listings.append(listing)

        return self._dedupe_listings(listings)

    def _listing_candidates(self, soup: BeautifulSoup) -> list[Tag]:
        selectors = [
            "article",
            "[class*=project]",
            "[class*=listing]",
            "[class*=job]",
            "[class*=post]",
            "[class*=card]",
            "li",
        ]
        candidates: list[Tag] = []
        for selector in selectors:
            for node in soup.select(selector):
                if isinstance(node, Tag) and self._node_has_listing_signal(node):
                    candidates.append(node)
        return candidates

    def _node_has_listing_signal(self, node: Tag) -> bool:
        text = node.get_text(" ", strip=True)
        return bool(extract_telegram_links(str(node)) or parse_date(text))

    async def _listing_from_node(self, node: Tag, page_url: str) -> ProjectListing | None:
        date_posted = self._extract_date(node)
        if not is_recent(date_posted, self.settings.recent_days):
            return None

        project_url = self._extract_project_url(node, page_url)
        project_name = self._extract_project_name(node) or project_url
        telegram_links = extract_telegram_links(str(node))

        if not telegram_links and project_url:
            telegram_links = await self._extract_telegram_links_from_detail(project_url)

        if not telegram_links:
            return None

        return ProjectListing(
            project_name=project_name,
            project_url=project_url,
            date_posted=date_posted,
            telegram_links=tuple(telegram_links),
        )

    def _extract_date(self, node: Tag) -> datetime | None:
        date_sources: list[str] = []
        for time_node in node.select("time"):
            if isinstance(time_node, Tag):
                date_sources.extend(
                    value for value in [
                        time_node.get("datetime"),
                        time_node.get("title"),
                        time_node.get_text(" ", strip=True),
                    ] if isinstance(value, str)
                )

        for attr in ("datetime", "data-date", "data-posted", "data-created", "title"):
            value = node.get(attr)
            if isinstance(value, str):
                date_sources.append(value)

        date_sources.append(node.get_text(" ", strip=True))

        for source in date_sources:
            parsed = parse_date(source)
            if parsed:
                return parsed
        return None

    def _extract_project_url(self, node: Tag, page_url: str) -> str:
        links = [a for a in node.select("a[href]") if isinstance(a, Tag)]
        for anchor in links:
            href = anchor.get("href")
            if isinstance(href, str) and not href.startswith(("mailto:", "tel:", "#")):
                absolute = urljoin(page_url, href)
                if not self._is_telegram_url(absolute):
                    return absolute
        return page_url

    def _extract_project_name(self, node: Tag) -> str | None:
        for selector in ("h1", "h2", "h3", "[class*=title]", "[class*=name]", "a"):
            target = node.select_one(selector)
            if target:
                text = target.get_text(" ", strip=True)
                if text:
                    return text[:250]
        text = node.get_text(" ", strip=True)
        return text[:120] if text else None

    async def _extract_telegram_links_from_detail(self, project_url: str) -> list[str]:
        try:
            html = await self._fetch(project_url)
        except Exception as exc:
            logger.warning("Failed to fetch project detail %s: %s", project_url, exc)
            return []
        return extract_telegram_links(html)

    def _find_next_page(self, soup: BeautifulSoup, page_url: str) -> str | None:
        rel_next = soup.select_one('a[rel="next"][href]')
        if isinstance(rel_next, Tag):
            return urljoin(page_url, str(rel_next.get("href")))

        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True).lower()
            if text in {"next", "next page", ">", ">>", "older", "more"}:
                return urljoin(page_url, str(anchor.get("href")))
        return None

    def _is_telegram_url(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        return netloc in {"t.me", "telegram.me", "telegram.dog"}

    def _dedupe_listings(self, listings: list[ProjectListing]) -> list[ProjectListing]:
        seen: set[tuple[str, str]] = set()
        unique: list[ProjectListing] = []
        for listing in listings:
            key = (listing.project_url, "|".join(listing.telegram_links))
            if key not in seen:
                seen.add(key)
                unique.append(listing)
        return unique
