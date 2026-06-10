from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator, ChannelParticipantsAdmins, User
import socks

from config import SESSION_DIR, Settings
from proxy import build_telethon_proxy
from utils import retry_async


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAdmin:
    telegram_link: str
    admin_name: str
    admin_username: str
    telegram_user_id: int | None
    role: str


class TelegramAdminExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = TelegramClient(
            str(Path(SESSION_DIR) / settings.session_name),
            settings.api_id,
            settings.api_hash,
            proxy=build_telethon_proxy(settings),
        )

    async def __aenter__(self) -> "TelegramAdminExtractor":
        await self.client.start(phone=self.settings.phone)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.client.disconnect()

    async def extract_many(self, links: list[str]) -> dict[str, list[TelegramAdmin]]:
        results: dict[str, list[TelegramAdmin]] = {}
        for link in links:
            try:
                results[link] = await self.extract_admins(link)
            except Exception as exc:
                logger.warning("Skipping Telegram link %s after failure: %s", link, exc)
                results[link] = []
        return results

    async def extract_admins(self, link: str) -> list[TelegramAdmin]:
        username_or_invite = self._entity_identifier(link)
        if not username_or_invite:
            logger.warning("Unsupported Telegram link format: %s", link)
            return []

        async def operation() -> list[TelegramAdmin]:
            return await self._extract_admins_once(link, username_or_invite)

        try:
            return await retry_async(
                operation,
                attempts=3,
                retry_exceptions=(socks.ProxyError, OSError, ConnectionError, TimeoutError),
                logger=logger,
            )
        except FloodWaitError as exc:
            await self._sleep_flood_wait(exc)
            try:
                return await self._extract_admins_once(link, username_or_invite)
            except Exception as retry_exc:
                logger.warning("Telegram retry failed for %s: %s", link, retry_exc)
                return []
        except Exception as exc:
            logger.warning("Telegram extraction failed for %s: %s", link, exc)
            return []

    async def _extract_admins_once(self, link: str, entity_identifier: str) -> list[TelegramAdmin]:
        try:
            entity = await self.client.get_entity(entity_identifier)
        except FloodWaitError as exc:
            await self._sleep_flood_wait(exc)
            entity = await self.client.get_entity(entity_identifier)

        admins: list[TelegramAdmin] = []
        offset = 0
        limit = 100

        while True:
            try:
                response = await self.client(
                    GetParticipantsRequest(
                        channel=entity,
                        filter=ChannelParticipantsAdmins(),
                        offset=offset,
                        limit=limit,
                        hash=0,
                    )
                )
            except FloodWaitError as exc:
                await self._sleep_flood_wait(exc)
                continue
            except RPCError as exc:
                logger.warning("Admins are not visible or accessible for %s: %s", link, exc)
                return admins

            user_by_id = {user.id: user for user in response.users if isinstance(user, User)}
            for participant in response.participants:
                user = user_by_id.get(participant.user_id)
                if not user or user.bot:
                    continue
                admins.append(self._admin_from_user(link, user, self._role_from_participant(participant)))

            if len(response.participants) < limit:
                break
            offset += len(response.participants)

        return self._dedupe_admins(admins)

    def _admin_from_user(self, link: str, user: User, role: str) -> TelegramAdmin:
        display_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
        return TelegramAdmin(
            telegram_link=link,
            admin_name=display_name,
            admin_username=f"@{user.username}" if user.username else "",
            telegram_user_id=user.id,
            role=role,
        )

    def _role_from_participant(self, participant) -> str:
        if isinstance(participant, ChannelParticipantCreator):
            return "admin"
        if isinstance(participant, ChannelParticipantAdmin):
            rank = (getattr(participant, "rank", None) or "").lower()
            if "mod" in rank:
                return "moderator"
            return "admin"
        return "unknown"

    async def _sleep_flood_wait(self, exc: FloodWaitError) -> None:
        wait_seconds = int(getattr(exc, "seconds", 0)) + 2
        logger.warning("Telegram FloodWaitError: sleeping for %s seconds", wait_seconds)
        await asyncio.sleep(wait_seconds)

    def _entity_identifier(self, link: str) -> str | None:
        parsed = urlparse(link)
        path = parsed.path.strip("/")
        if not path:
            return None
        if path.startswith(("+", "joinchat/")):
            logger.info("Skipping invite/private link because public admins are not available: %s", link)
            return None
        return path.split("/")[0]

    def _dedupe_admins(self, admins: list[TelegramAdmin]) -> list[TelegramAdmin]:
        seen: set[tuple[str, int | None, str]] = set()
        unique: list[TelegramAdmin] = []
        for admin in admins:
            key = (admin.telegram_link, admin.telegram_user_id, admin.admin_username)
            if key not in seen:
                seen.add(key)
                unique.append(admin)
        return unique
