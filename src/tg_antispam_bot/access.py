from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Chat, TelegramObject, Update
from cachetools import TTLCache

from .db import Database

logger = logging.getLogger(__name__)

LEAVE_RETRY_SECONDS = 60
LEAVE_CACHE_SIZE = 4096
GONE_STATUSES = {"left", "kicked"}
TEMPORARY_ERRORS = (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)


def update_chat(update: Update) -> Chat | None:
    for event in (
        update.message,
        update.edited_message,
        update.chat_member,
        update.my_chat_member,
    ):
        if event is not None:
            return event.chat
    if (
        update.callback_query is not None
        and update.callback_query.message is not None
    ):
        return update.callback_query.message.chat
    return None


class AccessControl:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        allowed_chat_ids: Iterable[int],
        fallback_chat_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bot = bot
        self.db = db
        self.fallback_chat_id = fallback_chat_id
        self._configured = frozenset(allowed_chat_ids)
        self._allowed = set(self._configured)
        self.on_fallback_moved: Callable[[int], None] | None = None
        self._leaving: TTLCache = TTLCache(
            maxsize=LEAVE_CACHE_SIZE, ttl=LEAVE_RETRY_SECONDS, timer=clock
        )

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self._allowed

    async def load(self) -> None:
        for old_chat_id, new_chat_id in await self.db.chat_migrations():
            if old_chat_id in self._configured:
                self._allowed.add(new_chat_id)
            if old_chat_id == self.fallback_chat_id:
                self._set_fallback(new_chat_id)

    async def sweep(self) -> None:
        for chat in await self.db.active_chats():
            if (
                not self.is_allowed(chat.chat_id)
                and chat.chat_id != self.fallback_chat_id
            ):
                await self.leave(chat.chat_id, chat.title)

    async def leave(self, chat_id: int, title: str | None) -> None:
        if chat_id in self._leaving:
            return
        self._leaving[chat_id] = True
        logger.warning(
            "leaving chat that is not in ALLOWED_CHAT_IDS",
            extra={"chat_id": chat_id, "title": title},
        )
        try:
            await self.bot.leave_chat(chat_id=chat_id)
        except TEMPORARY_ERRORS as exc:
            self._leaving.pop(chat_id, None)
            logger.warning(
                "could not leave chat",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            return
        except TelegramAPIError as exc:
            logger.info(
                "chat already unavailable",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
        await self.db.set_chat_active(chat_id, False, int(time.time()))

    def _set_fallback(self, chat_id: int) -> None:
        self.fallback_chat_id = chat_id
        if self.on_fallback_moved is not None:
            self.on_fallback_moved(chat_id)

    async def follow_fallback(self, new_chat_id: int) -> None:
        old_chat_id = self.fallback_chat_id
        if old_chat_id is None or old_chat_id == new_chat_id:
            return
        self._set_fallback(new_chat_id)
        await self.db.add_chat_migration(old_chat_id, new_chat_id)
        logger.warning(
            "fallback chat was upgraded, update FALLBACK_LOG_CHAT_ID",
            extra={"old_chat_id": old_chat_id, "new_chat_id": new_chat_id},
        )

    async def _follow_migration(self, update: Update, chat: Chat) -> bool:
        message = update.message
        if message is None:
            return False
        new_chat_id = message.migrate_to_chat_id
        moved_from_fallback = False
        if new_chat_id is not None and chat.id == self.fallback_chat_id:
            moved_from_fallback = True
            await self.follow_fallback(new_chat_id)
        elif (
            message.migrate_from_chat_id is not None
            and message.migrate_from_chat_id == self.fallback_chat_id
        ):
            await self.follow_fallback(chat.id)
        if message.migrate_to_chat_id is not None and self.is_allowed(chat.id):
            await self._allow_migrated(chat.id, message.migrate_to_chat_id)
        elif message.migrate_from_chat_id is not None and self.is_allowed(
            message.migrate_from_chat_id
        ):
            await self._allow_migrated(message.migrate_from_chat_id, chat.id)
        return moved_from_fallback

    async def _allow_migrated(self, old_chat_id: int, new_chat_id: int) -> None:
        await self.db.add_chat_migration(old_chat_id, new_chat_id)
        if new_chat_id in self._allowed:
            return
        self._allowed.add(new_chat_id)
        logger.warning(
            "allowed group was upgraded, add its new ID to ALLOWED_CHAT_IDS",
            extra={"old_chat_id": old_chat_id, "new_chat_id": new_chat_id},
        )

    async def admit(self, update: Update, chat: Chat) -> bool:
        left_fallback = await self._follow_migration(update, chat)
        if self.is_allowed(chat.id):
            return True
        if left_fallback:
            return False
        if chat.id == self.fallback_chat_id:
            return update.callback_query is not None
        member_update = update.my_chat_member
        if (
            member_update is not None
            and member_update.new_chat_member.status in GONE_STATUSES
        ):
            await self.db.set_chat_active(chat.id, False, int(time.time()))
            return False
        await self.leave(chat.id, chat.title)
        return False


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        chat = update_chat(event)
        if chat is None or chat.type == "private":
            return await handler(event, data)
        if await data["services"].access.admit(event, chat):
            return await handler(event, data)
        return None
