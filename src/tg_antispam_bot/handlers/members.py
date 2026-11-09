from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    ChatMemberBanned,
    ChatMemberRestricted,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.deep_linking import create_start_link

from ..admin import missing_rights
from ..render import DOT_GREEN, DOT_YELLOW, SUBSCRIBE_PAYLOAD_PREFIX
from ..services import Services

logger = logging.getLogger(__name__)

ADMIN_STATUSES = {"administrator", "creator"}
ABSENT_STATUSES = {"left", "kicked"}
GROUP_TYPES = {"group", "supergroup"}

router = Router(name="members")


def _sanction_lifted(member: Any) -> bool:
    if isinstance(member, ChatMemberBanned):
        return False
    if isinstance(member, ChatMemberRestricted):
        return member.can_send_messages
    return True


def _sync_member(event: ChatMemberUpdated, services: Services) -> None:
    statuses = {event.old_chat_member.status, event.new_chat_member.status}
    if statuses & ADMIN_STATUSES:
        services.admins.invalidate(event.chat.id)
    if _sanction_lifted(event.new_chat_member):
        services.engine.clear_sanction(
            event.chat.id, event.new_chat_member.user.id
        )


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, services: Services) -> None:
    _sync_member(event, services)
    await services.engine.handle_join(event)


@router.chat_member()
async def on_member_change(
    event: ChatMemberUpdated, services: Services
) -> None:
    _sync_member(event, services)


@router.my_chat_member()
async def on_my_chat_member(
    event: ChatMemberUpdated, services: Services, bot: Bot
) -> None:
    chat = event.chat
    now = services.engine.now()
    new_member = event.new_chat_member
    if chat.type == "private":
        if new_member.status == "kicked":
            await services.db.mark_user_blocked(chat.id, now)
        elif new_member.status == "member":
            await services.db.reactivate_blocked(chat.id, now)
        return
    if chat.type not in GROUP_TYPES:
        return
    services.admins.invalidate(chat.id)
    if new_member.status in ABSENT_STATUSES:
        await services.db.set_chat_active(chat.id, False, now)
        services.engine.forget_chat(chat.id)
        logger.info("bot removed from chat", extra={"chat_id": chat.id})
        return
    await services.db.upsert_chat(
        chat.id,
        chat.title or str(chat.id),
        chat.type,
        now,
        active=True,
    )
    newly_added = event.old_chat_member.status in ABSENT_STATUSES
    became_admin = (
        event.old_chat_member.status not in ADMIN_STATUSES
        and new_member.status in ADMIN_STATUSES
    )
    if newly_added or became_admin:
        await _send_setup_notice(event, services, bot)


async def _send_setup_notice(
    event: ChatMemberUpdated, services: Services, bot: Bot
) -> None:
    chat = event.chat
    adder_id = event.from_user.id
    missing = missing_rights(event.new_chat_member)
    link = await create_start_link(bot, f"{SUBSCRIBE_PAYLOAD_PREFIX}{chat.id}")
    title = escape(chat.title or str(chat.id))
    if missing:
        status = (
            f"{DOT_YELLOW} I am in <b>{title}</b> but I still need: "
            f"{', '.join(missing)}."
        )
    else:
        status = (
            f"{DOT_GREEN} I am active in <b>{title}</b> with the rights I need."
        )
    text = (
        f"{status}\n\n"
        "Turn on action alerts for this group by tapping the button below."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Subscribe to alerts", url=link)]
        ]
    )
    try:
        await bot.send_message(chat_id=adder_id, text=text, reply_markup=markup)
        return
    except TelegramForbiddenError:
        logger.info(
            "adder has not started the bot",
            extra={"chat_id": chat.id, "user_id": adder_id},
        )
    except TelegramAPIError as exc:
        logger.info(
            "setup notice DM failed",
            extra={"chat_id": chat.id, "user_id": adder_id, "error": str(exc)},
        )
    delivered = await services.ephemeral.send_to(
        chat.id, adder_id, text, markup
    )
    if not delivered:
        logger.warning(
            "setup notice undelivered, promote the bot to admin",
            extra={"chat_id": chat.id, "user_id": adder_id},
        )


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message, services: Services) -> None:
    old_id = message.chat.id
    new_id = message.migrate_to_chat_id
    if new_id is None:
        return
    await services.db.migrate_chat(old_id, new_id)
    services.rules.invalidate(old_id)
    services.rules.invalidate(new_id)
    services.admins.forget(old_id)
    services.engine.forget_chat(old_id)
    logger.info(
        "chat migrated", extra={"old_chat_id": old_id, "new_chat_id": new_id}
    )


@router.message(F.migrate_from_chat_id)
async def on_migrated_here(message: Message, services: Services) -> None:
    services.rules.invalidate(message.chat.id)
