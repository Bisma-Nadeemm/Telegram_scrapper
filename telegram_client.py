from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest, JoinChannelRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator, ChannelParticipantsAdmins, User
import socks

from config import SESSION_DIR, Settings
from proxy import build_telethon_proxy
from utils import extract_public_contacts, retry_async


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAdmin:
    telegram_link: str
    admin_name: str
    admin_username: str
    telegram_user_id: int | None
    role: str


@dataclass(frozen=True)
class TelegramGroupInfo:
    telegram_link: str
    channel_title: str = ""
    channel_username: str = ""
    channel_description: str = ""
    linked_contacts: tuple[str, ...] = ()
    pinned_messages: tuple[str, ...] = ()
    admins: tuple[TelegramAdmin, ...] = ()
    joined_for_visibility: bool = False


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

    async def extract_many(self, links: list[str]) -> dict[str, TelegramGroupInfo]:
        results: dict[str, TelegramGroupInfo] = {}
        for link in links:
            try:
                results[link] = await self.extract_group_info(link)
            except Exception as exc:
                logger.warning("Skipping Telegram link %s after failure: %s", link, exc)
                results[link] = TelegramGroupInfo(telegram_link=link)
        return results

    async def extract_admins(self, link: str) -> list[TelegramAdmin]:
        return list((await self.extract_group_info(link)).admins)

    async def extract_group_info(self, link: str) -> TelegramGroupInfo:
        username_or_invite = self._entity_identifier(link)
        if not username_or_invite:
            logger.warning("Unsupported Telegram link format: %s", link)
            return TelegramGroupInfo(telegram_link=link)

        async def operation() -> TelegramGroupInfo:
            return await self._extract_group_info_once(link, username_or_invite, allow_join=True)

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
                return await self._extract_group_info_once(link, username_or_invite, allow_join=True)
            except Exception as retry_exc:
                logger.warning("Telegram retry failed for %s: %s", link, retry_exc)
                return TelegramGroupInfo(telegram_link=link)
        except Exception as exc:
            logger.warning("Telegram extraction failed for %s: %s", link, exc)
            return TelegramGroupInfo(telegram_link=link)

    async def _extract_group_info_once(
        self,
        link: str,
        entity_identifier: str,
        *,
        allow_join: bool,
    ) -> TelegramGroupInfo:
        try:
            entity = await self.client.get_entity(entity_identifier)
        except FloodWaitError as exc:
            await self._sleep_flood_wait(exc)
            entity = await self.client.get_entity(entity_identifier)

        joined = False
        title = getattr(entity, "title", "") or ""
        username = getattr(entity, "username", "") or entity_identifier
        description = ""
        pinned_messages: list[str] = []
        linked_contacts: list[str] = []
        metadata_blocked = False

        try:
            description, pinned_messages = await self._extract_public_metadata(entity)
            linked_contacts.extend(extract_public_contacts(description))
            for message in pinned_messages:
                linked_contacts.extend(extract_public_contacts(message))
        except RPCError as exc:
            metadata_blocked = True
            logger.info("Metadata is not fully visible for %s: %s", link, exc)

        admins, admins_blocked = await self._extract_visible_admins(link, entity)
        if allow_join and (metadata_blocked or admins_blocked) and self._is_public_link(link):
            logger.info("Joining public Telegram entity only because admin visibility requires it: %s", link)
            joined = await self._join_public_entity(entity, link)
            if joined:
                try:
                    description, pinned_messages = await self._extract_public_metadata(entity)
                    linked_contacts.extend(extract_public_contacts(description))
                    for message in pinned_messages:
                        linked_contacts.extend(extract_public_contacts(message))
                except RPCError as exc:
                    logger.info("Metadata is still not fully visible after join for %s: %s", link, exc)
                admins, _ = await self._extract_visible_admins(link, entity)

        return TelegramGroupInfo(
            telegram_link=link,
            channel_title=title,
            channel_username=f"@{username}" if username and not str(username).startswith("@") else str(username),
            channel_description=description,
            linked_contacts=tuple(dict.fromkeys(linked_contacts)),
            pinned_messages=tuple(dict.fromkeys(pinned_messages)),
            admins=tuple(admins),
            joined_for_visibility=joined,
        )

    async def _extract_public_metadata(self, entity) -> tuple[str, list[str]]:
        full = await self.client(GetFullChannelRequest(channel=entity))
        full_chat = full.full_chat
        description = getattr(full_chat, "about", "") or ""
        pinned_messages: list[str] = []
        pinned_msg_id = getattr(full_chat, "pinned_msg_id", None)

        if pinned_msg_id:
            try:
                message = await self.client.get_messages(entity, ids=pinned_msg_id)
                text = getattr(message, "message", "") or ""
                if text:
                    pinned_messages.append(text)
            except FloodWaitError as exc:
                await self._sleep_flood_wait(exc)
            except RPCError as exc:
                logger.info("Pinned message is not visible: %s", exc)

        return description, pinned_messages

    async def _extract_visible_admins(self, link: str, entity) -> tuple[list[TelegramAdmin], bool]:
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
                return admins, True

            user_by_id = {user.id: user for user in response.users if isinstance(user, User)}
            for participant in response.participants:
                user = user_by_id.get(participant.user_id)
                if not user or user.bot:
                    continue
                admins.append(self._admin_from_user(link, user, self._role_from_participant(participant)))

            if len(response.participants) < limit:
                break
            offset += len(response.participants)

        return self._dedupe_admins(admins), False

    async def _join_public_entity(self, entity, link: str) -> bool:
        try:
            await self.client(JoinChannelRequest(channel=entity))
            return True
        except FloodWaitError as exc:
            await self._sleep_flood_wait(exc)
            return False
        except RPCError as exc:
            logger.info("Could not join public Telegram entity %s: %s", link, exc)
            return False

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

    def _is_public_link(self, link: str) -> bool:
        return self._entity_identifier(link) is not None

    def _dedupe_admins(self, admins: list[TelegramAdmin]) -> list[TelegramAdmin]:
        seen: set[tuple[str, int | None, str]] = set()
        unique: list[TelegramAdmin] = []
        for admin in admins:
            key = (admin.telegram_link, admin.telegram_user_id, admin.admin_username)
            if key not in seen:
                seen.add(key)
                unique.append(admin)
        return unique
