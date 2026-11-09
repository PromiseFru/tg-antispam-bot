from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import sys
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramUnauthorizedError,
)
from aiogram.types import ChatMemberUpdated
from aiogram.utils.token import TokenValidationError
from dotenv import dotenv_values

from .config import NO_CHATS, split_chat_ids

GROUP_TYPES = {"group", "supergroup"}
PRESENT_STATUSES = {"member", "administrator"}
POLL_SECONDS = 30
WAIT_SECONDS = 600

Ask = Callable[[str], str]
MakeBot = Callable[[str], Bot]


def read_env(path: Path) -> dict[str, str]:
    values = dotenv_values(path) if path.exists() else {}
    return {key: value for key, value in values.items() if value is not None}


def write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in values.items())
    os.replace(temporary, path)


def write_default_rules(path: Path) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    default = resources.files("tg_antispam_bot").joinpath("rules.yaml")
    path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def yes(answer: str, default: bool) -> bool:
    answer = answer.strip().lower()
    return default if not answer else answer in {"y", "yes"}


async def checked_token(
    token: str | None, ask_secret: Ask, make_bot: MakeBot
) -> tuple[str, str]:
    while True:
        if not token:
            token = ask_secret("Bot token from @BotFather: ").strip()
        try:
            bot = make_bot(token)
            try:
                me = await bot.me()
            finally:
                await bot.session.close()
        except TokenValidationError:
            print("That doesn't look like a bot token.")
        except TelegramUnauthorizedError:
            print("Telegram rejected that token.")
        else:
            return token, me.username or str(me.id)
        token = None


def added_group(update: ChatMemberUpdated) -> tuple[int, str] | None:
    chat = update.chat
    if chat.type not in GROUP_TYPES:
        return None
    if update.new_chat_member.status not in PRESENT_STATUSES:
        return None
    adder = update.from_user
    handle = f" @{adder.username}" if adder.username else ""
    title = chat.title or str(chat.id)
    return chat.id, f"{title} ({chat.id}), added by {adder.full_name}{handle}"


async def wait_for_groups(
    bot: Bot,
    ask: Ask,
    groups: list[int],
    approve: Callable[[int], None],
    wait_seconds: float,
) -> None:
    await bot.delete_webhook()
    deadline = time.monotonic() + wait_seconds
    offset = None
    while (remaining := deadline - time.monotonic()) > 0:
        updates = await bot.get_updates(
            offset=offset,
            timeout=max(1, int(min(POLL_SECONDS, remaining))),
            allowed_updates=["my_chat_member"],
        )
        for update in updates:
            offset = update.update_id + 1
            if update.my_chat_member is None:
                continue
            found = added_group(update.my_chat_member)
            if found is None or found[0] in groups:
                continue
            chat_id, label = found
            if yes(ask(f"Found {label}. Allow it? [Y/n] "), True):
                approve(chat_id)
            if not yes(ask("Add another group? [y/N] "), False):
                await bot.get_updates(offset=offset, timeout=0)
                return
            deadline = time.monotonic() + wait_seconds
    print("No group added in time. Run setup again to add one.")


async def run_setup(
    env_path: Path,
    db_path: str,
    rules_path: Path,
    ask: Ask,
    ask_secret: Ask,
    make_bot: MakeBot,
    wait_seconds: float = WAIT_SECONDS,
) -> None:
    values = read_env(env_path)
    token, username = await checked_token(
        values.get("BOT_TOKEN"), ask_secret, make_bot
    )
    print(f"Bot: @{username}")
    groups = [
        int(item) for item in split_chat_ids(values.get("ALLOWED_CHAT_IDS", ""))
    ]

    def save() -> None:
        values["ALLOWED_CHAT_IDS"] = ",".join(map(str, groups)) or NO_CHATS
        write_env(env_path, values)

    def approve(chat_id: int) -> None:
        groups.append(chat_id)
        save()

    values.update(
        BOT_TOKEN=token,
        DB_ENCRYPTION_KEY=values.get("DB_ENCRYPTION_KEY")
        or secrets.token_hex(32),
        DB_PATH=db_path,
        RULES_PATH=str(rules_path),
    )
    save()
    if write_default_rules(rules_path):
        print(f"Wrote {rules_path}")
    if not groups or yes(ask("Add a group now? [y/N] "), False):
        print(f"Add @{username} to your group as an admin now.")
        bot = make_bot(token)
        try:
            await wait_for_groups(bot, ask, groups, approve, wait_seconds)
        finally:
            await bot.session.close()
    print(f"Saved {env_path}")
    print(
        "Make sure the bot is an admin with Delete messages and Ban users, "
        "then send /setupalerts in each group to get alerts."
    )


def ask_secret(prompt: str) -> str:
    if sys.version_info >= (3, 14):
        return getpass.getpass(prompt, echo_char="*")
    return getpass.getpass(prompt)


def setup_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="tg-antispam-bot setup")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--db-path", default="tg-antispam-bot.db")
    parser.add_argument("--rules-path", type=Path, default=Path("rules.yaml"))
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            run_setup(
                args.env_file,
                args.db_path,
                args.rules_path,
                input,
                ask_secret,
                lambda token: Bot(token=token),
            )
        )
    except TelegramConflictError as exc:
        print(
            "Another program is receiving this bot's updates. "
            "Stop the running bot first.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except (KeyboardInterrupt, EOFError) as exc:
        print(
            f"\nSetup stopped. Anything confirmed is saved in {args.env_file}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
