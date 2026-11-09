from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import ChatPermissions

from .config import TIER_BAN, TIER_DELETE, TIER_RESTRICT
from .db import ACTION_BAN, ACTION_RAID_RESTRICT, ACTION_RESTRICT, Database

logger = logging.getLogger(__name__)

T = TypeVar("T")

DELETE_CHUNK_SIZE = 100
MAX_RETRY_ATTEMPTS = 3

MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

OPEN_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def with_retry(
    call: Callable[[], Awaitable[T]], attempts: int = MAX_RETRY_ATTEMPTS
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "rate limited", extra={"retry_after": exc.retry_after}
            )
            await asyncio.sleep(exc.retry_after)
    raise RuntimeError("retry loop exited without a result")


def chunked(
    items: list[int], size: int = DELETE_CHUNK_SIZE
) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class ApplyResult:
    deleted_count: int = 0
    until_ts: int | None = None
    errors: list[str] = field(default_factory=list)


class ActionExecutor:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db

    async def delete_messages(
        self, chat_id: int, message_ids: Iterable[int]
    ) -> tuple[int, list[str]]:
        ids = sorted(
            {message_id for message_id in message_ids if message_id > 0}
        )
        deleted = 0
        errors: list[str] = []
        for chunk in chunked(ids):
            try:
                await with_retry(
                    lambda chunk=chunk: self.bot.delete_messages(
                        chat_id=chat_id, message_ids=chunk
                    )
                )
                deleted += len(chunk)
            except TelegramAPIError as exc:
                logger.warning(
                    "bulk delete failed",
                    extra={
                        "chat_id": chat_id,
                        "count": len(chunk),
                        "error": str(exc),
                    },
                )
                errors.append(f"delete failed: {exc}")
        return deleted, errors

    async def apply(
        self,
        chat_id: int,
        user_id: int,
        tier: str,
        message_id: int,
        restrict_seconds: int,
        revoke_messages: bool,
        now: int,
    ) -> ApplyResult:
        bulk = tier != TIER_DELETE
        buffered = (
            await self.db.buffered_messages(chat_id, user_id) if bulk else []
        )
        deleted, errors = await self.delete_messages(
            chat_id, [*buffered, message_id]
        )
        result = ApplyResult(deleted_count=deleted, errors=errors)
        if not errors:
            if bulk:
                await self.db.clear_buffer(chat_id, user_id)
            else:
                await self.db.unbuffer_message(chat_id, user_id, message_id)
        if tier == TIER_RESTRICT:
            if user_id > 0:
                result.until_ts = now + restrict_seconds
                await self._restrict(chat_id, user_id, result.until_ts, result)
            else:
                await self._ban(chat_id, user_id, False, result)
        elif tier == TIER_BAN:
            await self._ban(chat_id, user_id, revoke_messages, result)
        return result

    async def ban_member(self, chat_id: int, user_id: int) -> ApplyResult:
        result = ApplyResult()
        await self._ban(chat_id, user_id, False, result)
        return result

    async def restrict_joiner(
        self, chat_id: int, user_id: int, until_ts: int
    ) -> ApplyResult:
        result = ApplyResult(until_ts=until_ts)
        await self._restrict(chat_id, user_id, until_ts, result)
        return result

    async def _restrict(
        self, chat_id: int, user_id: int, until_ts: int, result: ApplyResult
    ) -> None:
        try:
            await with_retry(
                lambda: self.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=MUTED_PERMISSIONS,
                    use_independent_chat_permissions=True,
                    until_date=until_ts,
                )
            )
        except TelegramAPIError as exc:
            logger.warning(
                "restrict failed",
                extra={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            result.errors.append(f"restrict failed: {exc}")

    async def _ban(
        self, chat_id: int, user_id: int, revoke: bool, result: ApplyResult
    ) -> None:
        try:
            if user_id < 0:
                await with_retry(
                    lambda: self.bot.ban_chat_sender_chat(
                        chat_id=chat_id, sender_chat_id=user_id
                    )
                )
            else:
                await with_retry(
                    lambda: self.bot.ban_chat_member(
                        chat_id=chat_id, user_id=user_id, revoke_messages=revoke
                    )
                )
        except TelegramAPIError as exc:
            logger.warning(
                "ban failed",
                extra={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            result.errors.append(f"ban failed: {exc}")

    async def lift(self, chat_id: int, user_id: int, action: str) -> None:
        if action == ACTION_BAN or (action == ACTION_RESTRICT and user_id < 0):
            if user_id < 0:
                await with_retry(
                    lambda: self.bot.unban_chat_sender_chat(
                        chat_id=chat_id, sender_chat_id=user_id
                    )
                )
            else:
                await with_retry(
                    lambda: self.bot.unban_chat_member(
                        chat_id=chat_id, user_id=user_id, only_if_banned=True
                    )
                )
        elif action in (ACTION_RESTRICT, ACTION_RAID_RESTRICT):
            chat = await with_retry(lambda: self.bot.get_chat(chat_id=chat_id))
            permissions = chat.permissions or OPEN_PERMISSIONS
            await with_retry(
                lambda: self.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=permissions,
                    use_independent_chat_permissions=True,
                )
            )
