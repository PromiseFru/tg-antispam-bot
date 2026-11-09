from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from ..admin import AdminLookupError
from ..db import PREF_MODES
from ..render import (
    DOT_GREEN,
    NO_GROUPS_TEXT,
    SUBSCRIBE_PAYLOAD_PREFIX,
    PrefCb,
    SubCb,
    groups_card,
    prefs_keyboard,
    prefs_text,
)
from ..services import Services

logger = logging.getLogger(__name__)

LOOKUP_CONCURRENCY = 5

router = Router(name="private")
router.message.filter(F.chat.type == "private")


async def admin_groups(
    services: Services, user_id: int
) -> list[tuple[int, str, bool]]:
    chats = [
        chat
        for chat in await services.db.active_chats()
        if services.access.is_allowed(chat.chat_id)
    ]
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def check(chat) -> tuple[int, str, bool] | None:
        async with semaphore:
            try:
                if not await services.admins.is_admin(chat.chat_id, user_id):
                    return None
            except AdminLookupError:
                return None
        subscribed = await services.db.is_subscribed(chat.chat_id, user_id)
        return (chat.chat_id, chat.title or str(chat.chat_id), subscribed)

    results = await asyncio.gather(*(check(chat) for chat in chats))
    return [row for row in results if row is not None]


async def _send_groups(
    message: Message,
    services: Services,
    bot: Bot,
    user_id: int,
    intro: str = "",
) -> None:
    rows = await admin_groups(services, user_id)
    if rows:
        await services.db.mark_started(user_id, services.engine.now())
    if not rows:
        await message.answer(
            f"{escape(intro)}\n\n{NO_GROUPS_TEXT}" if intro else NO_GROUPS_TEXT
        )
        return
    await bot.send_rich_message(
        chat_id=message.chat.id, rich_message=groups_card(rows, intro)
    )


async def _subscribe_from_link(
    services: Services, user_id: int, payload: str
) -> str:
    try:
        chat_id = int(payload[len(SUBSCRIBE_PAYLOAD_PREFIX) :])
    except ValueError:
        return "That subscribe link is not valid."
    if not services.access.is_allowed(chat_id):
        return "This bot is not available for that group."
    try:
        allowed = await services.admins.is_admin(chat_id, user_id, fresh=True)
    except AdminLookupError:
        return "I could not verify your admin status for that group."
    if not allowed:
        return "You are not an admin of that group, so alerts were not enabled."
    await services.db.set_subscription(
        chat_id, user_id, True, services.engine.now()
    )
    chat = await services.db.get_chat(chat_id)
    title = f": {chat.title}" if chat and chat.title else ""
    return f"{DOT_GREEN} Subscribed to alerts for that group{title}."


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, services: Services, bot: Bot
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    await services.db.reactivate_blocked(user_id, services.engine.now())
    intro = ""
    if command.args and command.args.startswith(SUBSCRIBE_PAYLOAD_PREFIX):
        intro = await _subscribe_from_link(services, user_id, command.args)
    await _send_groups(message, services, bot, user_id, intro)


@router.message(Command("groups"))
async def cmd_groups(message: Message, services: Services, bot: Bot) -> None:
    if message.from_user is None:
        return
    await _send_groups(message, services, bot, message.from_user.id)


@router.message(Command("prefs"))
async def cmd_prefs(message: Message, services: Services) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if not await admin_groups(services, user_id):
        await message.answer(NO_GROUPS_TEXT)
        return
    await services.db.mark_started(user_id, services.engine.now())
    mode = await services.db.get_pref(user_id)
    await message.answer(prefs_text(mode), reply_markup=prefs_keyboard(mode))


@router.callback_query(SubCb.filter())
async def on_subscribe(
    callback: CallbackQuery, callback_data: SubCb, services: Services, bot: Bot
) -> None:
    user_id = callback.from_user.id
    if callback_data.on:
        if not services.access.is_allowed(callback_data.chat_id):
            await callback.answer(
                "This bot is not available for that group.", show_alert=True
            )
            return
        try:
            allowed = await services.admins.is_admin(
                callback_data.chat_id, user_id, fresh=True
            )
        except AdminLookupError:
            await callback.answer(
                "Could not verify your admin status. Try again.",
                show_alert=True,
            )
            return
        if not allowed:
            await callback.answer(
                "You are not an admin of that group.", show_alert=True
            )
            return
    await services.db.set_subscription(
        callback_data.chat_id,
        user_id,
        bool(callback_data.on),
        services.engine.now(),
    )
    await callback.answer("Subscribed" if callback_data.on else "Unsubscribed")
    rows = await admin_groups(services, user_id)
    if isinstance(callback.message, Message):
        try:
            if rows:
                await bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    rich_message=groups_card(rows),
                )
            else:
                await bot.edit_message_text(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    text=NO_GROUPS_TEXT,
                )
        except TelegramBadRequest as exc:
            logger.debug("groups view not edited", extra={"error": str(exc)})


@router.callback_query(PrefCb.filter())
async def on_pref(
    callback: CallbackQuery, callback_data: PrefCb, services: Services
) -> None:
    if callback_data.mode not in PREF_MODES:
        await callback.answer()
        return
    if not await admin_groups(services, callback.from_user.id):
        await callback.answer(
            "You are not an admin of any group using this bot.",
            show_alert=True,
        )
        return
    await services.db.set_pref(callback.from_user.id, callback_data.mode)
    await callback.answer("Saved")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                prefs_text(callback_data.mode),
                reply_markup=prefs_keyboard(callback_data.mode),
            )
        except TelegramBadRequest as exc:
            logger.debug("prefs view not edited", extra={"error": str(exc)})
