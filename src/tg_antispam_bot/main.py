from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    ErrorEvent,
)
from pydantic import ValidationError
from pythonjsonlogger.json import JsonFormatter

from .access import AccessMiddleware
from .config import ConfigError, RulesFile, Settings, load_rules_file
from .db import Database, DatabaseKeyError
from .handlers import build_router
from .pattern_check import check_file_patterns
from .rules_store import RulesStore
from .services import Services, UpdateTracker, build_services
from .setup_wizard import setup_cli

logger = logging.getLogger("tg_antispam_bot")

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "chat_member",
    "my_chat_member",
    "callback_query",
]

GROUP_COMMAND_DESCRIPTIONS = (
    ("status", "Show bot rights, thresholds, raid mode and subscribers"),
    ("log", "Show recent actions"),
    ("antispam", "Turn anti-spam on or off"),
    ("threshold", "Set a tier threshold"),
    ("addrule", "Add a detector pattern"),
    ("delrule", "Remove a detector pattern"),
    ("trust", "Exempt a user"),
    ("untrust", "Remove a user's exemption"),
    ("raidmode", "Turn raid mode on or off"),
    ("setupalerts", "Get the DM alerts link"),
)
PRIVATE_COMMAND_DESCRIPTIONS = (
    ("start", "Choose groups to get alerts for"),
    ("groups", "List your groups"),
    ("prefs", "Choose which alerts you get"),
)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "level", "name": "logger"},
            json_ensure_ascii=False,
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


async def register_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command=name, description=description, is_ephemeral=True)
            for name, description in GROUP_COMMAND_DESCRIPTIONS
        ],
        scope=BotCommandScopeAllChatAdministrators(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command=name, description=description)
            for name, description in PRIVATE_COMMAND_DESCRIPTIONS
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )


async def on_error(event: ErrorEvent) -> bool:
    logger.error(
        "update handler failed",
        extra={"update_id": event.update.update_id},
        exc_info=event.exception,
    )
    return True


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["services"] = services
    dispatcher.update.outer_middleware(UpdateTracker())
    dispatcher.update.outer_middleware(AccessMiddleware())
    dispatcher.include_router(build_router())
    dispatcher.errors.register(on_error)
    return dispatcher


async def cleanup_loop(services: Services) -> None:
    settings = services.rules.global_rules
    while True:
        await asyncio.sleep(settings.cleanup_interval)
        try:
            await services.db.cleanup(
                now=int(time.time()),
                joins_retention=settings.joins_retention,
                recent_messages_retention=settings.recent_messages_retention,
                activity_retention=settings.activity_retention,
                actions_retention=settings.actions_retention,
                message_text_retention=settings.message_text_retention,
            )
        except Exception:
            logger.exception("cleanup failed")


async def serve(
    settings: Settings, database: Database, rules_file: RulesFile
) -> None:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    rules = RulesStore(database, rules_file, settings.rules_path)
    services = build_services(
        bot,
        database,
        rules,
        settings.allowed_chat_ids,
        settings.fallback_log_chat_id,
    )
    loop = asyncio.get_running_loop()
    cleanup_task: asyncio.Task[None] | None = None
    try:
        dispatcher = build_dispatcher(services)
        loop.add_signal_handler(signal.SIGHUP, rules.request_reload)
        cleanup_task = asyncio.create_task(cleanup_loop(services))
        await services.access.load()
        if not settings.allowed_chat_ids:
            logger.warning(
                "ALLOWED_CHAT_IDS is none, "
                "the bot will leave every group it is added to"
            )
        await services.access.sweep()
        await bot.delete_webhook()
        await register_commands(bot)
        me = await bot.me()
        logger.info(
            "bot started", extra={"username": me.username, "bot_id": me.id}
        )
        await dispatcher.start_polling(
            bot,
            allowed_updates=ALLOWED_UPDATES,
            handle_as_tasks=True,
            close_bot_session=False,
        )
    finally:
        loop.remove_signal_handler(signal.SIGHUP)
        if cleanup_task is not None:
            cleanup_task.cancel()
        await services.updates.drain()
        await services.stop()
        await bot.session.close()


async def run(settings: Settings) -> None:
    rules_file = load_rules_file(settings.rules_path)
    await check_file_patterns(rules_file)
    database = Database(
        settings.db_path, settings.db_encryption_key.get_secret_value()
    )
    await database.connect()
    try:
        await serve(settings, database, rules_file)
    finally:
        await database.close()
        logger.info("bot stopped")


def cli() -> None:
    if sys.argv[1:2] == ["setup"]:
        setup_cli(sys.argv[2:])
        return
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except (ConfigError, DatabaseKeyError) as exc:
        logger.error("configuration error", extra={"error": str(exc)})
        raise SystemExit(2) from exc


if __name__ == "__main__":
    cli()
