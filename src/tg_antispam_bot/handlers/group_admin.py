from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.deep_linking import create_start_link

from ..admin import AdminLookupError
from ..config import PATTERN_DETECTORS
from ..render import (
    SUBSCRIBE_PAYLOAD_PREFIX,
    fit_blocks,
    format_time,
)
from ..services import Services

logger = logging.getLogger(__name__)

MAX_TELEGRAM_ID = 2**52

GROUP_COMMANDS = (
    "antispam",
    "status",
    "log",
    "threshold",
    "addrule",
    "delrule",
    "trust",
    "untrust",
    "raidmode",
    "setupalerts",
)

router = Router(name="group_admin")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


class IsChatAdmin(Filter):
    async def __call__(self, message: Message, services: Services) -> bool:
        user = message.from_user
        if user is None or message.sender_chat is not None:
            return False
        try:
            return await services.admins.is_admin(message.chat.id, user.id)
        except AdminLookupError as exc:
            logger.warning(
                "admin check failed for command",
                extra={"chat_id": message.chat.id, "error": str(exc)},
            )
            return False


class IsAnonymousAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return (
            message.sender_chat is not None
            and message.sender_chat.id == message.chat.id
        )


class DeleteCommandMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        services = data["services"]
        if isinstance(event, Message):
            chat = event.chat
            await services.engine.remember_chat(
                chat.id, chat.title or str(chat.id), chat.type
            )
        try:
            return await handler(event, data)
        finally:
            if isinstance(event, Message):
                await services.ephemeral.delete_command(event)


router.message.middleware(DeleteCommandMiddleware())


@router.message(Command(*GROUP_COMMANDS), IsAnonymousAdmin())
async def anonymous_admin_command(message: Message) -> None:
    logger.info(
        "ignored command from anonymous admin, no private reply is possible",
        extra={"chat_id": message.chat.id},
    )


def _on_off(value: str | None) -> bool | None:
    if value is None:
        return None
    return {"on": True, "off": False}.get(value.strip().lower())


@router.message(Command("antispam"), IsChatAdmin())
async def cmd_antispam(
    message: Message, command: CommandObject, services: Services
) -> None:
    chat_id = message.chat.id
    parts = (command.args or "").split()
    enabled = _on_off(parts[1]) if len(parts) == 2 else None
    if parts and parts[0].lower() == "alerts" and enabled is not None:
        await services.db.set_setting(
            chat_id, "notifications.live_ephemeral_alerts", enabled
        )
        services.rules.invalidate(chat_id)
        await services.ephemeral.reply(
            message,
            f"Live ephemeral alerts are now {'on' if enabled else 'off'}.",
        )
        return
    enabled = _on_off(parts[0]) if len(parts) == 1 else None
    if enabled is None:
        await services.ephemeral.reply(
            message, "Usage: /antispam on|off or /antispam alerts on|off"
        )
        return
    await services.db.set_setting(chat_id, "enabled", enabled)
    services.rules.invalidate(chat_id)
    await services.ephemeral.reply(
        message, f"Anti-spam is now {'on' if enabled else 'off'}."
    )


@router.message(Command("status"), IsChatAdmin())
async def cmd_status(message: Message, services: Services) -> None:
    await services.ephemeral.reply(
        message, await services.moderation.status_card(message.chat.id)
    )


@router.message(Command("log"), IsChatAdmin())
async def cmd_log(
    message: Message, command: CommandObject, services: Services
) -> None:
    chat_id = message.chat.id
    rules = (await services.rules.get(chat_id)).rules
    limit = rules.notifications.log_limit
    if command.args and command.args.strip().isdecimal():
        limit = int(command.args.strip())
    card, markup = await services.moderation.log_view(chat_id, 0, limit)
    await services.ephemeral.reply(message, card, markup)


@router.message(Command("threshold"), IsChatAdmin())
async def cmd_threshold(
    message: Message, command: CommandObject, services: Services
) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[1].isdecimal():
        await services.ephemeral.reply(
            message, "Usage: /threshold delete|restrict|ban &lt;value&gt;"
        )
        return
    problem = await services.moderation.set_threshold(
        message.chat.id, parts[0], int(parts[1])
    )
    await services.ephemeral.reply(
        message,
        escape(problem)
        if problem
        else f"Threshold {escape(parts[0])} set to {parts[1]}.",
    )


def _rule_args(command: CommandObject) -> tuple[str, str] | None:
    parts = (command.args or "").strip().split(maxsplit=1)
    return (parts[0], parts[1].strip()) if len(parts) == 2 else None


@router.message(Command("addrule"), IsChatAdmin())
async def cmd_addrule(
    message: Message, command: CommandObject, services: Services
) -> None:
    args = _rule_args(command)
    if args is None:
        await services.ephemeral.reply(
            message,
            "Usage: /addrule &lt;detector&gt; &lt;regex&gt;\n"
            f"Detectors: {', '.join(PATTERN_DETECTORS)}",
        )
        return
    problem = await services.moderation.add_rule(message.chat.id, *args)
    await services.ephemeral.reply(
        message,
        escape(problem)
        if problem
        else f"Added to {escape(args[0])}: <code>{escape(args[1])}</code>",
    )


@router.message(Command("delrule"), IsChatAdmin())
async def cmd_delrule(
    message: Message, command: CommandObject, services: Services
) -> None:
    args = _rule_args(command)
    if args is None:
        rules = (await services.rules.get(message.chat.id)).rules
        lines = [
            "Usage: /delrule &lt;detector&gt; &lt;regex&gt;",
            "",
            "Active patterns:",
        ]
        for name in PATTERN_DETECTORS:
            patterns = getattr(rules.detectors, name).patterns
            listed = (
                ", ".join(f"<code>{escape(item)}</code>" for item in patterns)
                or "none"
            )
            lines.append(f"{name}: {listed}")
        await services.ephemeral.reply(message, fit_blocks(lines))
        return
    problem = await services.moderation.del_rule(message.chat.id, *args)
    await services.ephemeral.reply(
        message,
        escape(problem)
        if problem
        else f"Removed from {escape(args[0])}: <code>{escape(args[1])}</code>",
    )


def _target_id(message: Message, command: CommandObject) -> int | None:
    args = (command.args or "").strip()
    if args:
        try:
            target = int(args.split()[0])
        except ValueError:
            return None
        return target if abs(target) < MAX_TELEGRAM_ID else None
    replied = message.reply_to_message
    if replied is None or replied.forum_topic_created is not None:
        return None
    if replied.sender_chat is not None:
        return replied.sender_chat.id
    return replied.from_user.id if replied.from_user else None


@router.message(Command("trust"), IsChatAdmin())
async def cmd_trust(
    message: Message, command: CommandObject, services: Services
) -> None:
    target = _target_id(message, command)
    if target is None:
        await services.ephemeral.reply(
            message, "Reply to a message or pass a user ID: /trust 12345"
        )
        return
    await services.db.add_trusted(message.chat.id, target)
    services.engine.clear_sanction(message.chat.id, target)
    await services.ephemeral.reply(message, f"Trusted <code>{target}</code>.")


@router.message(Command("untrust"), IsChatAdmin())
async def cmd_untrust(
    message: Message, command: CommandObject, services: Services
) -> None:
    target = _target_id(message, command)
    if target is None:
        await services.ephemeral.reply(
            message, "Reply to a message or pass a user ID: /untrust 12345"
        )
        return
    removed = await services.db.remove_trusted(message.chat.id, target)
    text = (
        f"Removed trust for <code>{target}</code>."
        if removed
        else f"<code>{target}</code> was not trusted."
    )
    await services.ephemeral.reply(message, text)


@router.message(Command("raidmode"), IsChatAdmin())
async def cmd_raidmode(
    message: Message, command: CommandObject, services: Services
) -> None:
    enabled = _on_off(command.args)
    if enabled is None:
        await services.ephemeral.reply(message, "Usage: /raidmode on|off")
        return
    until = await services.engine.set_raid_mode(message.chat.id, enabled)
    text = (
        f"Raid mode is on until {format_time(until)}."
        if enabled
        else "Raid mode is off."
    )
    await services.ephemeral.reply(message, text)


@router.message(Command("setupalerts"), IsChatAdmin())
async def cmd_setupalerts(
    message: Message, services: Services, bot: Bot
) -> None:
    link = await create_start_link(
        bot, f"{SUBSCRIBE_PAYLOAD_PREFIX}{message.chat.id}"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Subscribe to alerts", url=link)]
        ]
    )
    await services.ephemeral.reply(
        message,
        f"Open this link to get action alerts for this group in a DM:\n{link}",
        markup,
    )
