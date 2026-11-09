from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from ..admin import (
    STATUS_ALREADY,
    STATUS_FAILED,
    STATUS_FORBIDDEN,
    STATUS_MISSING,
    STATUS_UNAVAILABLE,
    AdminLookupError,
)
from ..db import ActionRecord
from ..render import (
    LIST_PAGE_SIZE,
    SURFACE_EPHEMERAL,
    VERB_LABELS,
    ActionCb,
    CardCb,
    DigestListCb,
    DismissCb,
    LogPageCb,
    LogViewCb,
    action_keyboard,
    list_page_card,
    list_page_keyboard,
    log_detail_keyboard,
    log_size,
    resolved_label,
    rich_card,
)
from ..services import Services

logger = logging.getLogger(__name__)

DISMISS_FAILED = (
    "Telegram would not delete this card. You can delete it yourself."
)

router = Router(name="callbacks")

OUTCOME_ALERTS = {
    STATUS_MISSING: "That action no longer exists.",
    STATUS_FORBIDDEN: "Only current admins of this group can do that.",
    STATUS_UNAVAILABLE: "Could not verify your admin status. Try again.",
    STATUS_FAILED: "Telegram rejected the change. Check the bot's rights.",
}


def _message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    return message if isinstance(message, Message) else None


@router.callback_query(ActionCb.filter())
async def on_action(
    callback: CallbackQuery,
    callback_data: ActionCb,
    services: Services,
    bot: Bot,
) -> None:
    if callback_data.verb not in VERB_LABELS:
        await callback.answer()
        return
    message = _message(callback)
    is_ephemeral = callback_data.surface == SURFACE_EPHEMERAL
    skip = None
    if message is not None and not is_ephemeral:
        skip = (message.chat.id, message.message_id)

    outcome = await services.moderation.resolve(
        callback_data.action_id,
        callback_data.verb,
        callback.from_user.id,
        callback.from_user.full_name,
        skip_dm=skip,
    )
    alert = OUTCOME_ALERTS.get(outcome.status)
    if alert is not None:
        await callback.answer(alert, show_alert=True)
        return
    if outcome.status == STATUS_ALREADY:
        await callback.answer("Already resolved by another admin.")
    elif outcome.detail:
        await callback.answer(
            f"{VERB_LABELS[callback_data.verb]} saved, but Telegram would not "
            "lift the restriction. Check the bot's rights.",
            show_alert=True,
        )
    elif outcome.action is not None:
        await callback.answer(resolved_label(outcome.action))
    else:
        await callback.answer()

    action = outcome.action
    if action is None:
        return
    if is_ephemeral:
        rules = (await services.rules.get(action.chat_id)).rules
        await _show_log_detail(
            callback, services, action, 0, rules.notifications.log_limit
        )
        return
    if message is not None:
        title = await services.db.chat_title(action.chat_id)
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                rich_message=rich_card(
                    action, title, services.notify.preview_chars
                ),
                reply_markup=action_keyboard(action),
            )
        except TelegramBadRequest as exc:
            logger.debug("card not edited", extra={"error": str(exc)})


async def _verify_admin(
    callback: CallbackQuery, services: Services, chat_id: int
) -> bool:
    if not services.access.is_allowed(chat_id):
        await callback.answer(OUTCOME_ALERTS[STATUS_FORBIDDEN], show_alert=True)
        return False
    try:
        allowed = await services.admins.is_admin(
            chat_id, callback.from_user.id, fresh=True
        )
    except AdminLookupError:
        await callback.answer(
            OUTCOME_ALERTS[STATUS_UNAVAILABLE], show_alert=True
        )
        return False
    if not allowed:
        await callback.answer(OUTCOME_ALERTS[STATUS_FORBIDDEN], show_alert=True)
    return allowed


@router.callback_query(DismissCb.filter())
async def on_dismiss(
    callback: CallbackQuery,
    callback_data: DismissCb,
    services: Services,
    bot: Bot,
) -> None:
    message = _message(callback)
    if message is None:
        await callback.answer()
        return
    if message.chat.type != ChatType.PRIVATE:
        action = await services.db.get_action(callback_data.action_id)
        if action is None:
            await callback.answer(
                OUTCOME_ALERTS[STATUS_MISSING], show_alert=True
            )
            return
        if not await _verify_admin(callback, services, action.chat_id):
            return
    try:
        await bot.delete_message(
            chat_id=message.chat.id, message_id=message.message_id
        )
    except TelegramAPIError as exc:
        logger.info("card not dismissed", extra={"error": str(exc)})
        await callback.answer(DISMISS_FAILED, show_alert=True)
        return
    await callback.answer("Dismissed")


async def _show_log_page(
    callback: CallbackQuery,
    services: Services,
    chat_id: int,
    page: int,
    size: int,
) -> None:
    card, markup = await services.moderation.log_view(chat_id, page, size)
    await services.ephemeral.update_from_callback(
        callback, chat_id, card, markup
    )


async def _show_log_detail(
    callback: CallbackQuery,
    services: Services,
    action: ActionRecord,
    page: int,
    size: int,
) -> None:
    title = await services.db.chat_title(action.chat_id)
    await services.ephemeral.update_from_callback(
        callback,
        action.chat_id,
        rich_card(action, title, services.notify.preview_chars),
        log_detail_keyboard(action, page, log_size(size)),
    )


@router.callback_query(LogViewCb.filter())
async def on_log_view(
    callback: CallbackQuery, callback_data: LogViewCb, services: Services
) -> None:
    chat_id = callback_data.chat_id
    if not await _verify_admin(callback, services, chat_id):
        return
    action = await services.db.get_action(callback_data.action_id)
    if action is None or action.chat_id != chat_id:
        await callback.answer(OUTCOME_ALERTS[STATUS_MISSING], show_alert=True)
        return
    await callback.answer()
    await _show_log_detail(
        callback,
        services,
        action,
        max(0, callback_data.page),
        callback_data.size,
    )


@router.callback_query(LogPageCb.filter())
async def on_log_page(
    callback: CallbackQuery, callback_data: LogPageCb, services: Services
) -> None:
    chat_id = callback_data.chat_id
    if not await _verify_admin(callback, services, chat_id):
        return
    await callback.answer()
    await _show_log_page(
        callback,
        services,
        chat_id,
        max(0, callback_data.page),
        callback_data.size,
    )


@router.callback_query(DigestListCb.filter())
async def on_digest_list(
    callback: CallbackQuery,
    callback_data: DigestListCb,
    services: Services,
    bot: Bot,
) -> None:
    chat_id = callback_data.chat_id
    if not await _verify_admin(callback, services, chat_id):
        return
    page = max(0, callback_data.page)
    actions = await services.db.recent_actions(
        chat_id, LIST_PAGE_SIZE, page * LIST_PAGE_SIZE
    )
    total = await services.db.count_actions(chat_id)
    title = await services.db.chat_title(chat_id)
    card = list_page_card(title, actions, page, total)
    markup = list_page_keyboard(chat_id, actions, page, total)
    await callback.answer()
    message = _message(callback)
    if callback_data.new or message is None:
        await bot.send_rich_message(
            chat_id=message.chat.id if message else callback.from_user.id,
            rich_message=card,
            reply_markup=markup,
        )
        return
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            rich_message=card,
            reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        logger.debug("list page not edited", extra={"error": str(exc)})


@router.callback_query(CardCb.filter())
async def on_card(
    callback: CallbackQuery, callback_data: CardCb, services: Services, bot: Bot
) -> None:
    action = await services.db.get_action(callback_data.action_id)
    if action is None:
        await callback.answer(OUTCOME_ALERTS[STATUS_MISSING], show_alert=True)
        return
    if not await _verify_admin(callback, services, action.chat_id):
        return
    await callback.answer()
    title = await services.db.chat_title(action.chat_id)
    message = _message(callback)
    target = message.chat.id if message else callback.from_user.id
    try:
        sent = await bot.send_rich_message(
            chat_id=target,
            rich_message=rich_card(
                action, title, services.notify.preview_chars
            ),
            reply_markup=action_keyboard(action),
        )
    except TelegramAPIError as exc:
        logger.info("card not sent", extra={"error": str(exc)})
        return
    await services.db.add_action_dm(action.id, target, sent.message_id)
