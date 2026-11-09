from __future__ import annotations

import asyncio
import resource
import sys
from pathlib import Path

import pytest

from conftest import (
    CHAT_ID,
    SHIPPED_RULES,
    TEST_RULES,
)
from tg_antispam_bot.config import (
    ConfigError,
    load_rules_file,
    parse_rules_document,
)
from tg_antispam_bot.pattern_check import (
    check_file_patterns,
    check_user_pattern,
)
from tg_antispam_bot.rules_store import RulesStore

ROOT = Path(__file__).resolve().parent.parent


class TestRegexBombs:
    @pytest.mark.parametrize(
        "pattern",
        ["(a{4000}){4000}", "((a{100}){100}){100}", "(x{9999}){9999}"],
    )
    async def test_bomb_is_rejected_without_growing_this_process(self, pattern):
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        assert await check_user_pattern(pattern) == "that regex is too complex"
        grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        assert grown < 50 * 1024

    async def test_event_loop_keeps_running_during_check(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await check_user_pattern("(a{4000}){4000}")
        task.cancel()
        assert ticks >= 3

    @pytest.mark.parametrize(
        "pattern,expected",
        [("", "empty"), ("(", "invalid regex"), ("x" * 201, "longer than")],
    )
    async def test_bad_patterns_are_explained(self, pattern, expected):
        problem = await check_user_pattern(pattern)
        assert problem is not None and expected in problem

    async def test_probe_does_not_receive_secrets(self, monkeypatch):
        captured = {}
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            captured.update(kwargs)
            return await real(*args, **kwargs)

        monkeypatch.setenv("BOT_TOKEN", "secret")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        await check_user_pattern("a")
        assert captured["env"] == {}

    async def test_addrule_rejects_bomb_and_saves_nothing(self, services):
        problem = await services.moderation.add_rule(
            CHAT_ID, "text_pattern", "(a{4000}){4000}"
        )
        assert problem == "that regex is too complex"
        assert await services.db.get_settings(CHAT_ID) == {}

    async def test_pattern_over_the_tighter_budget_is_rejected(self):
        problem = await check_user_pattern("(a{2000}){100}")
        assert problem == "that regex is too complex"

    @pytest.mark.parametrize(
        "pattern",
        [
            "^dhm_",
            r"free\s+crypto",
            "(foo|bar)+",
            "(a+)+",
            "(a{100}){100}",
            "|".join(f"word{i}" for i in range(28))[:200],
            r"\\b(?:airdrop|free\\s*crypto|promo\\s*bot)\\b",
            "[a-z]{1,100}",
        ],
    )
    async def test_realistic_patterns_fit_the_budget(self, pattern):
        assert await check_user_pattern(pattern) is None


class TestFilePatternChecks:
    def rules_file(self, patterns, chat_patterns=None):
        document = {
            "defaults": {"detectors": {"text_pattern": {"patterns": patterns}}}
        }
        if chat_patterns is not None:
            document["chats"] = {
                str(CHAT_ID): {
                    "detectors": {"link_pattern": {"patterns": chat_patterns}}
                }
            }
        return parse_rules_document(document)

    async def test_good_patterns_pass(self):
        await check_file_patterns(self.rules_file(["^a", "b+"], ["c"]))

    async def test_shipped_rules_pass(self):
        await check_file_patterns(load_rules_file(SHIPPED_RULES))

    async def test_bomb_in_defaults_is_named(self):
        with pytest.raises(ConfigError, match="too complex"):
            await check_file_patterns(self.rules_file(["(a{4000}){4000}"]))

    async def test_bomb_in_group_overlay_is_found(self):
        with pytest.raises(ConfigError, match="too complex"):
            await check_file_patterns(
                self.rules_file(["^a"], ["(a{4000}){4000}"])
            )

    async def test_invalid_regex_is_rejected(self):
        with pytest.raises(ConfigError, match="invalid regex"):
            await check_file_patterns(self.rules_file(["("]))

    async def test_long_patterns_are_allowed_in_the_file(self):
        await check_file_patterns(self.rules_file(["a" * 500]))

    async def reloadable(self, db, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("defaults:\n  tiers:\n    delete: 11\n")
        return path, RulesStore(db, load_rules_file(path), str(path))

    async def test_reload_with_bomb_keeps_current_rules(self, db, tmp_path):
        path, store = await self.reloadable(db, tmp_path)
        path.write_text(
            "defaults:\n  tiers:\n    delete: 22\n  detectors:\n"
            "    text_pattern:\n      patterns: ['(a{4000}){4000}']\n"
        )
        assert await store.reload() is False
        rules = (await store.get(CHAT_ID)).rules
        assert rules.tiers.delete == 11
        assert rules.detectors.text_pattern.patterns == []

    async def test_request_reload_applies_in_background(self, db, tmp_path):
        path, store = await self.reloadable(db, tmp_path)
        path.write_text("defaults:\n  tiers:\n    delete: 22\n")
        store.request_reload()
        ticks = 0
        while (await store.get(CHAT_ID)).rules.tiers.delete != 22:
            ticks += 1
            assert ticks < 500
            store_cache_cleared = asyncio.sleep(0.01)
            await store_cache_cleared
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 22

    async def test_event_loop_keeps_running_during_reload(self, db, tmp_path):
        path, store = await self.reloadable(db, tmp_path)
        path.write_text(
            "defaults:\n  detectors:\n    text_pattern:\n"
            "      patterns: ['(a{4000}){4000}']\n"
        )
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        task = asyncio.create_task(ticker())
        await store.reload()
        task.cancel()
        assert ticks >= 2


class TestProbeFailures:
    def fake_python(self, tmp_path, monkeypatch, script):
        path = tmp_path / "fake-python"
        path.write_text(f"#!/bin/sh\ncat >/dev/null\n{script}\n")
        path.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(path))

    async def test_crash_is_not_called_an_invalid_regex(
        self, tmp_path, monkeypatch, caplog
    ):
        self.fake_python(tmp_path, monkeypatch, "echo boom >&2; exit 1")
        problem = await check_user_pattern("valid")
        assert problem == "could not check that regex, see the bot's log"
        assert "pattern probe crashed" in caplog.text

    async def test_killed_by_a_limit_is_too_complex(
        self, tmp_path, monkeypatch
    ):
        self.fake_python(tmp_path, monkeypatch, "kill -9 $$")
        assert await check_user_pattern("x") == "that regex is too complex"

    async def test_crash_stops_startup_with_the_right_message(
        self, tmp_path, monkeypatch
    ):
        self.fake_python(tmp_path, monkeypatch, "exit 1")
        with pytest.raises(ConfigError, match="could not check"):
            await check_file_patterns(parse_rules_document(TEST_RULES))
