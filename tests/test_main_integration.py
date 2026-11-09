from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import (
    Update,
)
from aiohttp import web
from pydantic import SecretStr

import tg_antispam_bot.main as main_module
from conftest import (
    BOT_ID,
    CHAT_ID,
    DHM_SPAM_TEXT,
    FAKE_TOKEN,
    OWNER_ID,
    SPAMMER_ID,
    TEST_DB_KEY,
    log_action,
    mention_entities,
    shared_dispatcher,
)
from tg_antispam_bot.config import (
    GlobalRules,
    Settings,
)
from tg_antispam_bot.db import Database
from tg_antispam_bot.services import UpdateTracker

STRANGER_CHAT_ID = -1007777


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_update_id = 1
        self.done = asyncio.Event()
        self.message_id = 100
        self.port = 0

    def named(self, name):
        return [body for method, body in self.calls if method == name]

    def admin(self, user_id, is_bot=False):
        return {
            "status": "creator" if not is_bot else "administrator",
            "user": {
                "id": user_id,
                "is_bot": is_bot,
                "first_name": f"U{user_id}",
            },
            "is_anonymous": False,
            **(
                {
                    "can_be_edited": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_manage_video_chats": False,
                    "can_restrict_members": True,
                    "can_promote_members": False,
                    "can_change_info": False,
                    "can_invite_users": True,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                    "can_send_welcome_messages": False,
                }
                if is_bot
                else {}
            ),
        }

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        body = dict(await request.post())
        self.calls.append((method, body))
        result = await self.respond(method, body)
        return web.json_response({"ok": True, "result": result})

    async def respond(self, method: str, body: dict):
        if method == "getMe":
            return {
                "id": BOT_ID,
                "is_bot": True,
                "first_name": "Bot",
                "username": "dekuspam_bot",
            }
        if method == "getUpdates":
            if self.updates:
                batch, self.updates = self.updates, []
                return batch
            await asyncio.sleep(0.05)
            return []
        if method == "getChatAdministrators":
            return [self.admin(OWNER_ID), self.admin(BOT_ID, is_bot=True)]
        if method == "getChatMember":
            return self.admin(BOT_ID, is_bot=True)
        if method in ("sendMessage", "sendRichMessage"):
            self.message_id += 1
            return {
                "message_id": self.message_id,
                "date": int(time.time()),
                "chat": {"id": int(body["chat_id"]), "type": "private"},
                "text": body.get("text", ""),
            }
        if method == "banChatMember":
            self.done.set()
        return True

    def add_update(self, **fields):
        self.updates.append({"update_id": self.next_update_id, **fields})
        self.next_update_id += 1


def message_payload(text, user_id, message_id, entities=None, **extra):
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Test Group"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Someone"},
        "text": text,
        "entities": entities or [],
        **extra,
    }


@pytest.fixture
async def telegram():
    fake = FakeTelegram()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.port = runner.addresses[0][1]
    yield fake
    await runner.cleanup()


async def test_full_process_over_http(telegram, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server = TelegramAPIServer.from_base(f"http://127.0.0.1:{telegram.port}")

    def patched_bot(token, default):
        return Bot(
            token=token, default=default, session=AiohttpSession(api=server)
        )

    monkeypatch.setattr(main_module, "Bot", patched_bot)
    monkeypatch.setattr(main_module, "build_dispatcher", shared_dispatcher)

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "defaults:\n"
        "  tiers:\n    delete: 50\n"
        "  detectors:\n"
        "    mention_pattern:\n      patterns: ['^dhm_']\n"
    )
    settings = Settings(
        bot_token=SecretStr(FAKE_TOKEN),
        db_encryption_key=SecretStr(TEST_DB_KEY),
        allowed_chat_ids=frozenset({CHAT_ID}),
        db_path=str(tmp_path / "bot.db"),
        rules_path=str(rules_path),
    )

    entities = [
        {"type": entity.type, "offset": entity.offset, "length": entity.length}
        for entity in mention_entities(DHM_SPAM_TEXT)
    ]
    stranger = message_payload(DHM_SPAM_TEXT, SPAMMER_ID, 54, entities)
    stranger["chat"] = {
        "id": STRANGER_CHAT_ID,
        "type": "supergroup",
        "title": "Stranger",
    }
    telegram.add_update(message=stranger)
    telegram.add_update(
        message=message_payload(DHM_SPAM_TEXT, SPAMMER_ID, 55, entities)
    )

    task = asyncio.create_task(main_module.run(settings))
    await asyncio.wait_for(telegram.done.wait(), timeout=10)

    telegram.add_update(
        message=message_payload(
            "/status",
            OWNER_ID,
            0,
            [{"type": "bot_command", "offset": 0, "length": 7}],
            ephemeral_message_id=77,
        )
    )
    for _ in range(100):
        if telegram.named("sendRichMessage"):
            break
        await asyncio.sleep(0.05)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=15)

    names = [method for method, _ in telegram.calls]
    assert names.index("deleteWebhook") < names.index("getUpdates")

    allowed = json.loads(telegram.named("getUpdates")[0]["allowed_updates"])
    assert allowed == [
        "message",
        "edited_message",
        "chat_member",
        "my_chat_member",
        "callback_query",
    ]

    commands = telegram.named("setMyCommands")
    group_commands = json.loads(commands[0]["commands"])
    assert all(command["is_ephemeral"] is True for command in group_commands)
    assert json.loads(commands[0]["scope"]) == {
        "type": "all_chat_administrators"
    }

    assert [int(body["chat_id"]) for body in telegram.named("leaveChat")] == [
        STRANGER_CHAT_ID
    ]
    assert [
        int(body["chat_id"]) for body in telegram.named("deleteMessages")
    ] == [CHAT_ID]
    assert json.loads(telegram.named("deleteMessages")[0]["message_ids"]) == [
        55
    ]
    assert int(telegram.named("banChatMember")[0]["user_id"]) == SPAMMER_ID

    reply = telegram.named("sendRichMessage")[0]
    assert (
        json.loads(reply["ephemeral_message_parameters"])["receiver_user_id"]
        == OWNER_ID
    )
    assert json.loads(reply["reply_parameters"])["ephemeral_message_id"] == 77
    assert "Anti-spam" in reply["rich_message"]
    assert "deleteMessage" not in names


def test_bad_settings_exit_cleanly_without_leaking(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("DB_ENCRYPTION_KEY", "hunter2-not-a-hex-key")
    with pytest.raises(SystemExit) as caught:
        main_module.cli()
    assert caught.value.code == 2
    assert "hunter2" not in capsys.readouterr().err


def test_logs_are_json_with_extra_fields(capsys):
    main_module.configure_logging("INFO")
    try:
        logging.getLogger("tg_antispam_bot.test").info(
            "spam detected", extra={"chat_id": -5}
        )
        line = capsys.readouterr().out.strip().splitlines()[-1]
    finally:
        logging.getLogger().handlers.clear()
    assert json.loads(line) == {
        "level": "INFO",
        "logger": "tg_antispam_bot.test",
        "message": "spam detected",
        "chat_id": -5,
    }


def run_real_process(tmp_path, rules="{}", **env):
    full_env = {
        "PATH": os.environ.get("PATH", ""),
        "DB_PATH": str(tmp_path / "bot.db"),
        "RULES_PATH": str(tmp_path / "rules.yaml"),
        "DB_ENCRYPTION_KEY": TEST_DB_KEY,
        "ALLOWED_CHAT_IDS": str(CHAT_ID),
        **env,
    }
    (tmp_path / "rules.yaml").write_text(rules)
    return subprocess.run(
        [sys.executable, "-m", "tg_antispam_bot.main"],
        env=full_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


SETTING_NAMES = (
    "BOT_TOKEN",
    "DB_ENCRYPTION_KEY",
    "ALLOWED_CHAT_IDS",
    "DB_PATH",
    "RULES_PATH",
    "FALLBACK_LOG_CHAT_ID",
    "LOG_LEVEL",
)


@pytest.fixture
def run_cli(tmp_path, monkeypatch, capsys):
    def run(rules="{}", **env):
        monkeypatch.chdir(tmp_path)
        for name in SETTING_NAMES:
            monkeypatch.delenv(name, raising=False)
        (tmp_path / "rules.yaml").write_text(rules)
        values = {
            "DB_PATH": str(tmp_path / "bot.db"),
            "RULES_PATH": str(tmp_path / "rules.yaml"),
            "DB_ENCRYPTION_KEY": TEST_DB_KEY,
            "ALLOWED_CHAT_IDS": str(CHAT_ID),
            "BOT_TOKEN": FAKE_TOKEN,
            **env,
        }
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        try:
            with pytest.raises(SystemExit) as caught:
                main_module.cli()
        finally:
            logging.getLogger().handlers.clear()
        output = capsys.readouterr()
        return caught.value.code, output.out + output.err

    return run


def test_malformed_token_exits(run_cli):
    code, output = run_cli(BOT_TOKEN="not-a-token")
    assert code == 2
    assert "bot_token" in output
    assert "not-a-token" not in output


def test_wrong_database_key_exits_instead_of_hanging(tmp_path):
    first = Database(str(tmp_path / "bot.db"), TEST_DB_KEY)

    async def seed():
        await first.connect()
        await first.close()

    asyncio.run(seed())
    result = run_real_process(
        tmp_path, BOT_TOKEN=FAKE_TOKEN, DB_ENCRYPTION_KEY="cd" * 32
    )
    assert result.returncode == 2
    assert "key is wrong" in result.stdout


async def test_cleanup_loop_applies_configured_retention(db):
    now = int(time.time())
    await db.bump_message_count(CHAT_ID, 1, now - 2000)
    await db.bump_message_count(CHAT_ID, 2, now - 500)
    old = await log_action(db, now=now - 2000)
    recent = await log_action(db, now=now - 500)
    global_rules = GlobalRules.model_validate(
        {"activity_retention": "1000s", "actions_retention": "1000s"}
    ).model_copy(update={"cleanup_interval": 0.01})
    services = cast(
        Any,
        SimpleNamespace(
            db=db, rules=SimpleNamespace(global_rules=global_rules)
        ),
    )
    task = asyncio.create_task(main_module.cleanup_loop(services))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if await db.get_action(old.id) is None:
            break
    task.cancel()
    assert await db.get_action(old.id) is None
    assert await db.get_action(recent.id) is not None
    assert await db.message_count(CHAT_ID, 1) == 0
    assert await db.message_count(CHAT_ID, 2) == 1


async def test_startup_failure_closes_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(main_module, "Bot", explode)
    (tmp_path / "rules.yaml").write_text("{}")
    settings = Settings(
        bot_token=SecretStr(FAKE_TOKEN),
        db_encryption_key=SecretStr(TEST_DB_KEY),
        allowed_chat_ids=frozenset({CHAT_ID}),
        db_path=str(tmp_path / "bot.db"),
        rules_path=str(tmp_path / "rules.yaml"),
    )
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError):
        await main_module.run(settings)
    leftover = [
        thread
        for thread in set(threading.enumerate()) - before
        if not thread.daemon
    ]
    assert leftover == []


def test_missing_allowlist_exits_instead_of_leaving_groups(run_cli):
    code, output = run_cli(ALLOWED_CHAT_IDS="")
    assert code == 2
    assert "allowed_chat_ids" in output


class NoPolling:
    async def start_polling(self, *args, **kwargs):
        return None


async def test_restart_leaves_stale_groups_but_keeps_upgraded_ones(
    telegram, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    server = TelegramAPIServer.from_base(f"http://127.0.0.1:{telegram.port}")

    def patched_bot(token, default):
        return Bot(
            token=token, default=default, session=AiohttpSession(api=server)
        )

    monkeypatch.setattr(main_module, "Bot", patched_bot)
    monkeypatch.setattr(
        main_module, "build_dispatcher", lambda services: NoPolling()
    )

    upgraded_id = -1004444
    seeded = Database(str(tmp_path / "bot.db"), TEST_DB_KEY)
    await seeded.connect()
    now = int(time.time())
    await seeded.upsert_chat(
        STRANGER_CHAT_ID, "Removed from list", "supergroup", now
    )
    await seeded.upsert_chat(upgraded_id, "Upgraded", "supergroup", now)
    await seeded.upsert_chat(CHAT_ID, "Listed", "supergroup", now)
    await seeded.add_chat_migration(CHAT_ID, upgraded_id)
    await seeded.close()

    (tmp_path / "rules.yaml").write_text("{}")
    settings = Settings(
        bot_token=SecretStr(FAKE_TOKEN),
        db_encryption_key=SecretStr(TEST_DB_KEY),
        allowed_chat_ids=frozenset({CHAT_ID}),
        db_path=str(tmp_path / "bot.db"),
        rules_path=str(tmp_path / "rules.yaml"),
    )
    await asyncio.wait_for(main_module.run(settings), timeout=15)

    assert [int(body["chat_id"]) for body in telegram.named("leaveChat")] == [
        STRANGER_CHAT_ID
    ]


@pytest.mark.parametrize(
    "pattern,message",
    [("(a{4000}){4000}", "too complex"), ("(", "invalid regex")],
)
def test_bad_file_pattern_stops_startup(run_cli, tmp_path, pattern, message):
    rules = json.dumps(
        {"defaults": {"detectors": {"text_pattern": {"patterns": [pattern]}}}}
    )
    code, output = run_cli(rules=rules)
    assert code == 2
    assert message in output
    assert not (tmp_path / "bot.db").exists()


def test_bad_group_file_pattern_stops_startup(run_cli):
    rules = json.dumps(
        {
            "chats": {
                str(CHAT_ID): {
                    "detectors": {
                        "link_pattern": {"patterns": ["(a{4000}){4000}"]}
                    }
                }
            }
        }
    )
    code, output = run_cli(rules=rules)
    assert code == 2
    assert "too complex" in output


async def _no_commands(bot):
    return None


def _stub_bot(token, default):
    async def noop(*args, **kwargs):
        return None

    async def me():
        return SimpleNamespace(username="x", id=1)

    return cast(
        Any,
        SimpleNamespace(
            delete_webhook=noop,
            me=me,
            session=SimpleNamespace(close=noop),
        ),
    )


class TestShutdown:
    async def test_drain_waits_for_running_handlers(self, services):
        finished = []

        async def handler(event, data):
            await asyncio.sleep(0.05)
            finished.append(True)

        tracker = UpdateTracker()
        services.updates = tracker
        data = {"services": services}
        task = asyncio.create_task(tracker(handler, Update(update_id=1), data))
        await asyncio.sleep(0)
        await tracker.drain()
        assert finished == [True]
        assert task.done()

    async def test_drain_gives_up_at_time_limit(self, services):
        release = asyncio.Event()

        async def handler(event, data):
            await release.wait()

        tracker = UpdateTracker()
        services.updates = tracker
        data = {"services": services}
        task = asyncio.create_task(tracker(handler, Update(update_id=1), data))
        await asyncio.sleep(0)
        started = time.monotonic()
        await tracker.drain(time_limit=0.05)
        assert time.monotonic() - started < 1
        release.set()
        await task

    async def test_nothing_running_returns_immediately(self):
        await UpdateTracker().drain()

    async def test_in_flight_ban_is_logged_before_database_closes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rules.yaml").write_text("{}")
        path = str(tmp_path / "bot.db")
        settings = Settings(
            bot_token=SecretStr(FAKE_TOKEN),
            db_encryption_key=SecretStr(TEST_DB_KEY),
            allowed_chat_ids=frozenset({CHAT_ID}),
            db_path=path,
            rules_path=str(tmp_path / "rules.yaml"),
        )
        spawned = []

        class ReturnsWhileHandlerRuns:
            def __init__(self, services):
                self.services = services

            async def start_polling(self, *args, **kwargs):
                services = self.services

                async def handler(event, data):
                    await asyncio.sleep(0.1)
                    await log_action(services.db, user_id=1, action="delete")

                spawned.append(
                    asyncio.create_task(
                        services.updates(
                            handler,
                            Update(update_id=1),
                            {"services": services},
                        )
                    )
                )
                await asyncio.sleep(0)

        monkeypatch.setattr(
            main_module, "build_dispatcher", ReturnsWhileHandlerRuns
        )
        monkeypatch.setattr(main_module, "register_commands", _no_commands)
        monkeypatch.setattr(main_module, "Bot", _stub_bot)
        await asyncio.wait_for(main_module.run(settings), timeout=15)
        assert spawned[0].exception() is None
        database = Database(path, TEST_DB_KEY)
        await database.connect()
        try:
            assert len(await database.recent_actions(CHAT_ID, 5)) == 1
        finally:
            await database.close()


async def test_sighup_reloads_rules_in_the_running_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "rules.yaml"
    path.write_text("defaults:\n  tiers:\n    delete: 11\n")
    settings = Settings(
        bot_token=SecretStr(FAKE_TOKEN),
        db_encryption_key=SecretStr(TEST_DB_KEY),
        allowed_chat_ids=frozenset({CHAT_ID}),
        db_path=str(tmp_path / "bot.db"),
        rules_path=str(path),
    )
    seen = []

    class SendsSighup:
        def __init__(self, services):
            self.services = services

        async def start_polling(self, *args, **kwargs):
            rules = self.services.rules
            seen.append((await rules.get(CHAT_ID)).rules.tiers.delete)
            path.write_text("defaults:\n  tiers:\n    delete: 22\n")
            os.kill(os.getpid(), signal.SIGHUP)
            for _ in range(200):
                await asyncio.sleep(0.01)
                current = (await rules.get(CHAT_ID)).rules.tiers.delete
                if current == 22:
                    break
            seen.append((await rules.get(CHAT_ID)).rules.tiers.delete)

    monkeypatch.setattr(main_module, "build_dispatcher", SendsSighup)
    monkeypatch.setattr(main_module, "register_commands", _no_commands)
    monkeypatch.setattr(main_module, "Bot", _stub_bot)
    await asyncio.wait_for(main_module.run(settings), timeout=15)
    assert seen == [11, 22]
