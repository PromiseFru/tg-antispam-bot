from __future__ import annotations

import os
import stat
import sys

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import ChatMemberLeft, Update, User

from conftest import (
    BOT_ID,
    FAKE_TOKEN,
    OWNER_ID,
    administrator,
    bot_member_event,
    make_chat,
    make_user,
)
from tg_antispam_bot import main, setup_wizard
from tg_antispam_bot.setup_wizard import read_env, run_setup

GROUP_ID = -1004352626486
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="Bot")


def joined(chat_id, chat_type="supergroup", present=True):
    member = (
        administrator(BOT_USER) if present else ChatMemberLeft(user=BOT_USER)
    )
    chat = make_chat(chat_id, f"Chat {chat_id}", chat_type)
    return bot_member_event(member, adder=OWNER_ID, chat=chat)


def answers(*replies):
    queue = list(replies)
    return lambda prompt: queue.pop(0)


def serve_updates(session, *events):
    batches = [
        [Update(update_id=i, my_chat_member=e) for i, e in enumerate(events, 1)]
    ]
    session.on(GetUpdates, lambda m: batches.pop(0) if batches else [])


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "config" / ".env", tmp_path / "config" / "rules.yaml"


@pytest.fixture
def make_bot(session):
    return lambda token: Bot(token=token, session=session)


@pytest.fixture
def setup(paths, make_bot):
    async def run(ask, secret=lambda _: FAKE_TOKEN, wait_seconds=600):
        env, rules = paths
        await run_setup(
            env, "/data/bot.db", rules, ask, secret, make_bot, wait_seconds
        )
        return read_env(env)

    return run


async def test_first_run_captures_the_group_and_writes_config(
    paths, session, setup, capsys
):
    serve_updates(session, joined(GROUP_ID))
    previous = os.umask(0o022)
    try:
        values = await setup(answers("y", "n"))
    finally:
        os.umask(previous)
    env, rules = paths
    assert values["BOT_TOKEN"] == FAKE_TOKEN
    assert values["ALLOWED_CHAT_IDS"] == str(GROUP_ID)
    assert len(bytes.fromhex(values["DB_ENCRYPTION_KEY"])) == 32
    assert values["DB_PATH"] == "/data/bot.db"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "mention_pattern" in rules.read_text()
    assert session.calls(GetUpdates)[-1].offset == 2
    assert f"Added {GROUP_ID}" in capsys.readouterr().out


async def test_only_new_groups_the_admin_approves_are_added(
    paths, session, setup
):
    env, _ = paths
    env.parent.mkdir()
    env.write_text('ALLOWED_CHAT_IDS="-5"\n')
    serve_updates(
        session,
        joined(-7, chat_type="channel"),
        joined(-8, present=False),
        joined(-5),
        joined(-9),
        joined(-10),
    )
    values = await setup(answers("y", "n", "y", "y", "n"))
    assert values["ALLOWED_CHAT_IDS"] == "-5,-10"


async def test_prompt_names_who_added_the_bot(session, setup):
    serve_updates(session, joined(GROUP_ID))
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return "n"

    await setup(ask)
    assert make_user(OWNER_ID).full_name in prompts[0]


async def test_rerun_keeps_secrets_settings_and_rules(paths, session, setup):
    env, rules = paths
    env.parent.mkdir()
    key = "ab" * 32
    env.write_text(
        f"BOT_TOKEN={FAKE_TOKEN}\nDB_ENCRYPTION_KEY={key}\n"
        f"ALLOWED_CHAT_IDS={GROUP_ID}\nLOG_LEVEL=DEBUG\n"
    )
    env.chmod(0o644)
    rules.write_text("{}")
    values = await setup(answers("n"))
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert values["DB_ENCRYPTION_KEY"] == key
    assert values["ALLOWED_CHAT_IDS"] == str(GROUP_ID)
    assert values["LOG_LEVEL"] == "DEBUG"
    assert rules.read_text() == "{}"
    assert session.calls(GetUpdates) == []


async def test_rerun_can_add_another_group(paths, session, setup):
    env, _ = paths
    env.parent.mkdir()
    env.write_text(f"BOT_TOKEN={FAKE_TOKEN}\nALLOWED_CHAT_IDS={GROUP_ID}\n")
    serve_updates(session, joined(-9))
    values = await setup(answers("y", "y", "n"))
    assert values["ALLOWED_CHAT_IDS"] == f"{GROUP_ID},-9"


async def test_bad_tokens_are_asked_for_again(session, setup):
    serve_updates(session, joined(GROUP_ID))
    tokens = iter(["nonsense", "999:" + "x" * 35, FAKE_TOKEN])
    session.on(
        GetMe,
        lambda m: (
            TelegramUnauthorizedError(m, "no")
            if len(session.calls(GetMe)) == 1
            else None
        ),
    )
    values = await setup(answers("y", "n"), secret=lambda _: next(tokens))
    assert values["BOT_TOKEN"] == FAKE_TOKEN
    assert next(tokens, None) is None


async def test_timeout_saves_the_token_without_groups(session, setup):
    values = await setup(answers(), wait_seconds=0)
    assert values["BOT_TOKEN"] == FAKE_TOKEN
    assert values["ALLOWED_CHAT_IDS"] == "none"


async def test_interrupting_keeps_groups_already_allowed(paths, session, setup):
    serve_updates(session, joined(GROUP_ID))
    replies = iter(["y"])

    def ask(prompt):
        reply = next(replies, None)
        if reply is None:
            raise KeyboardInterrupt
        return reply

    with pytest.raises(KeyboardInterrupt):
        await setup(ask)
    assert read_env(paths[0])["ALLOWED_CHAT_IDS"] == str(GROUP_ID)


@pytest.mark.parametrize(
    "error",
    [KeyboardInterrupt(), TelegramConflictError(GetMe(), "conflict")],
)
def test_cli_exits_cleanly_on_interrupt_or_running_bot(monkeypatch, error):
    async def fail(*args):
        raise error

    monkeypatch.setattr(setup_wizard, "run_setup", fail)
    with pytest.raises(SystemExit) as caught:
        setup_wizard.setup_cli([])
    assert caught.value.code == 1


def test_setup_subcommand_reaches_the_wizard(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tg-antispam-bot", "setup", "--help"])
    with pytest.raises(SystemExit) as caught:
        main.cli()
    assert caught.value.code == 0
    assert "--env-file" in capsys.readouterr().out
