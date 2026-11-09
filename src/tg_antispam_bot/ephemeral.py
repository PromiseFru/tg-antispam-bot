from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    ReplyParameters,
)

from .admin import AdminCache, AdminLookupError
from .db import ActionRecord, Database
from .render import NO_PREVIEW, alert_text
from .rules_store import RulesStore

logger = logging.getLogger(__name__)

ALERT_CONCURRENCY = 5
INVALID_REPLY_ERROR = "REPLY_TO_INVALID"

Content = str | InputRichMessage


class EphemeralTools:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        rules: RulesStore,
        admins: AdminCache,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bot = bot
        self.db = db
        self.rules = rules
        self.admins = admins
        self.clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cooldown_until: dict[int, float] = {}

    async def reply(
        self,
        message: Message,
        content: Content,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        user = message.from_user
        if user is None or message.sender_chat is not None:
            return False
        reply_parameters = None
        if message.ephemeral_message_id is not None:
            reply_parameters = ReplyParameters(
                ephemeral_message_id=message.ephemeral_message_id
            )
        return await self._send(
            message.chat.id, user.id, content, markup, reply_parameters
        )

    async def send_to(
        self,
        chat_id: int,
        user_id: int,
        content: Content,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        return await self._send(chat_id, user_id, content, markup, None)

    async def _send(
        self,
        chat_id: int,
        user_id: int,
        content: Content,
        markup: InlineKeyboardMarkup | None,
        reply_parameters: ReplyParameters | None,
        callback_query_id: str | None = None,
    ) -> bool:
        parameters = EphemeralMessageParameters(
            receiver_user_id=user_id, callback_query_id=callback_query_id
        )
        try:
            try:
                await self._deliver(
                    chat_id, content, markup, reply_parameters, parameters
                )
            except TelegramBadRequest as exc:
                if reply_parameters is None or INVALID_REPLY_ERROR not in str(
                    exc
                ):
                    raise
                logger.info(
                    "ephemeral reply target gone, sending without it",
                    extra={"chat_id": chat_id, "user_id": user_id},
                )
                await self._deliver(chat_id, content, markup, None, parameters)
        except TelegramAPIError as exc:
            logger.info(
                "ephemeral send failed",
                extra={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )
            return False
        return True

    async def _deliver(
        self,
        chat_id: int,
        content: Content,
        markup: InlineKeyboardMarkup | None,
        reply_parameters: ReplyParameters | None,
        parameters: EphemeralMessageParameters,
    ) -> None:
        if isinstance(content, str):
            await self.bot.send_message(
                chat_id=chat_id,
                text=content,
                reply_markup=markup,
                reply_parameters=reply_parameters,
                link_preview_options=NO_PREVIEW,
                ephemeral_message_parameters=parameters,
            )
            return
        await self.bot.send_rich_message(
            chat_id=chat_id,
            rich_message=content,
            reply_markup=markup,
            reply_parameters=reply_parameters,
            ephemeral_message_parameters=parameters,
        )

    async def update_from_callback(
        self,
        callback: CallbackQuery,
        chat_id: int,
        content: InputRichMessage,
        markup: InlineKeyboardMarkup | None,
    ) -> bool:
        message = callback.message
        ephemeral_id = getattr(message, "ephemeral_message_id", None)
        if ephemeral_id is not None:
            try:
                await self.bot.edit_ephemeral_message_text(
                    chat_id=chat_id,
                    receiver_user_id=callback.from_user.id,
                    ephemeral_message_id=ephemeral_id,
                    rich_message=content,
                    reply_markup=markup,
                )
                return True
            except TelegramAPIError as exc:
                if "not modified" in str(exc):
                    return True
                logger.info(
                    "ephemeral edit failed, sending a new message",
                    extra={"chat_id": chat_id, "error": str(exc)},
                )
        return await self._send(
            chat_id,
            callback.from_user.id,
            content,
            markup,
            None,
            callback_query_id=callback.id,
        )

    async def delete_command(self, message: Message) -> None:
        if message.ephemeral_message_id is not None or message.message_id <= 0:
            return
        try:
            await self.bot.delete_message(
                chat_id=message.chat.id, message_id=message.message_id
            )
        except TelegramAPIError as exc:
            logger.info(
                "could not delete command",
                extra={"chat_id": message.chat.id, "error": str(exc)},
            )

    def submit(self, action: ActionRecord) -> None:
        task = asyncio.create_task(self._alert(action))
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "ephemeral alert task failed",
                extra={"error": repr(task.exception())},
            )

    async def _alert(self, action: ActionRecord) -> None:
        chat_id = action.chat_id
        rules = (await self.rules.get(chat_id)).rules.notifications
        if not rules.live_ephemeral_alerts:
            return
        if self._cooldown_until.get(chat_id, 0) > self.clock():
            return
        try:
            admins = await self.admins.get_admins(chat_id)
        except AdminLookupError:
            return
        text = alert_text(action, await self.db.chat_title(chat_id))
        semaphore = asyncio.Semaphore(ALERT_CONCURRENCY)

        async def deliver(user_id: int) -> None:
            if self._cooldown_until.get(chat_id, 0) > self.clock():
                return
            async with semaphore:
                try:
                    await self._deliver(
                        chat_id,
                        text,
                        None,
                        None,
                        EphemeralMessageParameters(receiver_user_id=user_id),
                    )
                except TelegramRetryAfter as exc:
                    self._cooldown_until[chat_id] = (
                        self.clock() + exc.retry_after
                    )
                except TelegramAPIError as exc:
                    logger.debug(
                        "live alert not delivered",
                        extra={
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "error": str(exc),
                        },
                    )

        await asyncio.gather(
            *(
                deliver(user_id)
                for user_id, member in admins.items()
                if not member.user.is_bot
            )
        )

    async def join(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
