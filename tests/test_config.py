from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import tg_antispam_bot.main as main_module
from conftest import (
    CHAT_ID,
    FAKE_TOKEN,
    SHIPPED_RULES,
    TEST_DB_KEY,
    TEST_RULES,
    rich_text,
)
from tg_antispam_bot.admin import ModerationService
from tg_antispam_bot.config import (
    CasRule,
    ChatRules,
    ConfigError,
    GlobalRules,
    NewMemberRule,
    NotificationRules,
    RaidRule,
    RaidRules,
    Settings,
    load_rules_file,
    parse_chat_rules,
    parse_duration,
    parse_rules_document,
)
from tg_antispam_bot.rules_store import RulesStore

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_rules_file_is_valid():
    parsed = load_rules_file(SHIPPED_RULES)
    rules = parse_chat_rules(parsed.defaults)
    assert rules.detectors.mention_pattern.patterns == ["^dhm_"]
    assert rules.detectors.cas.enabled is False


ENV_NAMES = (
    "BOT_TOKEN",
    "DB_ENCRYPTION_KEY",
    "ALLOWED_CHAT_IDS",
    "DB_PATH",
    "RULES_PATH",
    "FALLBACK_LOG_CHAT_ID",
    "LOG_LEVEL",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:secret-token")
    monkeypatch.setenv("DB_ENCRYPTION_KEY", TEST_DB_KEY)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "-1001,-1002")
    return monkeypatch


def test_activity_retention_defaults_to_90_days():
    parsed = parse_rules_document(TEST_RULES)
    assert parsed.global_rules.activity_retention == 90 * 86400


def test_activity_retention_is_configurable():
    parsed = parse_rules_document({"global": {"activity_retention": "1h"}})
    assert parsed.global_rules.activity_retention == 3600


def test_activity_retention_must_be_positive():
    with pytest.raises(ConfigError):
        parse_rules_document({"global": {"activity_retention": "0s"}})


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("1d", 86400),
        ("2w", 1209600),
        ("90d", 7776000),
        ("1h30m", 5400),
        ("1.5h", 5400),
        ("0s", 0),
    ],
)
def test_durations_become_seconds(value, expected):
    assert parse_duration(value) == expected
    assert type(parse_duration(value)) is int


@pytest.mark.parametrize(
    "value",
    [
        "1M",
        "1Y",
        "1y",
        "1mo",
        "1H",
        "1D",
        "5",
        "abc",
        "",
        "1 d",
        "-5m",
        "1d!",
        60,
        1.5,
        True,
        None,
    ],
)
def test_bad_durations_are_rejected_with_a_hint(value):
    with pytest.raises(ValueError, match="lowercase s, m, h, d or w"):
        parse_duration(value)


def test_month_is_not_silently_read_as_a_minute():
    with pytest.raises(ConfigError, match="not a valid duration"):
        parse_rules_document({"defaults": {"restrict_duration": "1M"}})


def test_fractional_seconds_only_fit_fractional_settings():
    assert parse_duration("1.5s") == 1.5
    with pytest.raises(ConfigError):
        parse_rules_document({"defaults": {"restrict_duration": "90.5s"}})
    parsed = parse_rules_document({"global": {"digest_edit_interval": "1.5s"}})
    assert parsed.global_rules.digest_edit_interval == 1.5


@pytest.mark.parametrize(
    "document",
    [
        {"defaults": {"restrict_duration": 3600}},
        {"defaults": {"raid_mode": {"duration": 1800}}},
        {"defaults": {"detectors": {"raid": {"window": 60}}}},
        {"global": {"cleanup_interval": 3600}},
        {"global": {"digest_edit_interval": 3.0}},
        {"chats": {"-100": {"restrict_duration": 3600}}},
    ],
)
def test_durations_without_a_unit_are_rejected(document):
    with pytest.raises(ConfigError, match="not a valid duration"):
        parse_rules_document(document)


def test_duration_limits_apply_after_parsing():
    with pytest.raises(ConfigError):
        parse_rules_document({"defaults": {"restrict_duration": "30s"}})
    with pytest.raises(ConfigError):
        parse_rules_document({"defaults": {"restrict_duration": "400d"}})


def test_shipped_rules_match_built_in_defaults():
    parsed = load_rules_file(SHIPPED_RULES)
    assert parsed.global_rules == GlobalRules()
    shipped = parse_chat_rules(parsed.defaults)
    assert shipped.restrict_duration == ChatRules().restrict_duration
    assert shipped.raid_mode == RaidRules()
    assert shipped.notifications == NotificationRules()
    assert shipped.detectors.new_member == NewMemberRule()
    assert shipped.detectors.raid == RaidRule()
    assert shipped.detectors.cas == CasRule()


def test_settings_read_environment(env):
    env.setenv("DB_PATH", "/var/lib/x.db")
    env.setenv("FALLBACK_LOG_CHAT_ID", "-100")
    settings = Settings()
    assert settings.db_path == "/var/lib/x.db"
    assert settings.fallback_log_chat_id == -100
    assert settings.bot_token.get_secret_value() == "123:secret-token"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("-1001", {-1001}),
        ("-1001, -1002 ,", {-1001, -1002}),
        ("none", set()),
        (" NONE ", set()),
    ],
)
def test_allowed_chat_ids_parse(env, value, expected):
    env.setenv("ALLOWED_CHAT_IDS", value)
    assert Settings().allowed_chat_ids == expected


@pytest.mark.parametrize("value", ["abc", "-1001,abc", "1001", "0"])
def test_allowed_chat_ids_reject_bad_values(env, value):
    env.setenv("ALLOWED_CHAT_IDS", value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", [None, ""])
def test_allowed_chat_ids_cannot_be_missing_or_empty(env, value):
    if value is None:
        env.delenv("ALLOWED_CHAT_IDS")
    else:
        env.setenv("ALLOWED_CHAT_IDS", value)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_require_token(env):
    env.delenv("BOT_TOKEN")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "token", ["not-a-token", "abc:def", "123:", "123:has space"]
)
def test_malformed_token_is_rejected_without_echoing_it(env, token):
    env.setenv("BOT_TOKEN", token)
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert token not in str(caught.value)


def test_settings_require_encryption_key(env):
    env.delenv("DB_ENCRYPTION_KEY")
    with pytest.raises(ValidationError):
        Settings()


def test_empty_fallback_is_treated_as_unset(env):
    env.setenv("FALLBACK_LOG_CHAT_ID", "")
    assert Settings().fallback_log_chat_id is None


def test_settings_reject_bad_fallback(env):
    env.setenv("FALLBACK_LOG_CHAT_ID", "abc")
    with pytest.raises(ValidationError):
        Settings()


def test_bad_key_is_rejected_without_echoing_it(env):
    env.setenv("DB_ENCRYPTION_KEY", "hunter2-not-a-hex-key")
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert "hunter2" not in str(caught.value)


def test_secrets_are_masked_in_repr(env):
    settings = Settings()
    assert "secret-token" not in repr(settings)
    assert TEST_DB_KEY not in repr(settings)


@pytest.mark.parametrize(
    "document",
    [
        {"unknown": 1},
        {"defaults": {"bogus": 1}},
        {"defaults": {"tiers": {"delete": 90, "delete_and_restrict": 70}}},
        {"defaults": {"tiers": {"delete": "high"}}},
        {"defaults": {"detectors": {"nope": {}}}},
        {"defaults": {"detectors": {"mention_count": {"threshold": 0}}}},
        {"defaults": {"enabled": 1}},
        {"defaults": {"restrict_duration": "5s"}},
        {"global": {"notify_global_per_second": 30}},
        {"global": {"notify_per_admin_interval": "0.5s"}},
        {"chats": {"not-a-chat": {}}},
        {"chats": {"-100": {"tiers": {"delete": 9999}}}},
    ],
)
def test_invalid_documents_are_rejected(document):
    with pytest.raises(ConfigError):
        parse_rules_document(document)


class TestLayering:
    async def test_chat_overlay_over_defaults(self, db):
        file = parse_rules_document(
            {
                "chats": {
                    str(CHAT_ID): {"tiers": {"delete": 10}, "enabled": False}
                }
            }
        )
        store = RulesStore(db, file)
        ruleset = await store.get(CHAT_ID)
        assert ruleset.rules.tiers.delete == 10
        assert ruleset.rules.enabled is False
        assert ruleset.rules.tiers.delete_and_ban == 100

    async def test_sqlite_settings_win_over_file(self, db):
        file = parse_rules_document(
            {"chats": {str(CHAT_ID): {"tiers": {"delete": 10}}}}
        )
        store = RulesStore(db, file)
        await db.set_setting(CHAT_ID, "tiers.delete", 20)
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 20

    async def test_file_durations_survive_group_settings(self, db):
        file = parse_rules_document(
            {
                "defaults": {"raid_mode": {"duration": "2h"}},
                "chats": {str(CHAT_ID): {"restrict_duration": "3h"}},
            }
        )
        await db.set_setting(CHAT_ID, "tiers.delete", 20)
        await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["x"])
        rules = (await RulesStore(db, file).get(CHAT_ID)).rules
        assert rules.tiers.delete == 20
        assert rules.restrict_duration == 3 * 3600
        assert rules.raid_mode.duration == 2 * 3600
        assert rules.detectors.text_pattern.patterns == ["x"]

    async def test_group_pattern_changes_keep_file_patterns(self, db):
        file = parse_rules_document(
            {
                "defaults": {
                    "detectors": {"text_pattern": {"patterns": ["a", "b"]}}
                }
            }
        )
        await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["c"])
        await db.set_setting(CHAT_ID, "pattern_del.text_pattern", ["a"])
        rules = (await RulesStore(db, file).get(CHAT_ID)).rules
        assert rules.detectors.text_pattern.patterns == ["b", "c"]

    async def test_pattern_add_and_remove(self, db):
        store = RulesStore(db, parse_rules_document(TEST_RULES))
        await db.set_setting(CHAT_ID, "pattern_add.mention_pattern", ["^spm_"])
        await db.set_setting(CHAT_ID, "pattern_del.mention_pattern", ["^dhm_"])
        ruleset = await store.get(CHAT_ID)
        assert ruleset.rules.detectors.mention_pattern.patterns == ["^spm_"]

    async def test_bad_sqlite_settings_fall_back_to_file(self, db):
        store = RulesStore(db, parse_rules_document(TEST_RULES))
        await db.set_setting(CHAT_ID, "tiers.delete", 5000)
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 50

    async def test_disabled_detector_is_not_built(self, db):
        file = parse_rules_document(
            {"defaults": {"detectors": {"raid": {"enabled": False}}}}
        )
        ruleset = await RulesStore(db, file).get(CHAT_ID)
        assert "raid" not in {detector.name for detector in ruleset.detectors}

    async def test_reload_applies_new_file(self, db, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("defaults:\n  tiers:\n    delete: 11\n")
        store = RulesStore(db, load_rules_file(path), str(path))
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 11
        path.write_text("defaults:\n  tiers:\n    delete: 22\n")
        assert await store.reload() is True
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 22

    async def test_failed_reload_keeps_current_rules(self, db, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("defaults:\n  tiers:\n    delete: 11\n")
        store = RulesStore(db, load_rules_file(path), str(path))
        path.write_text("defaults: [broken")
        assert await store.reload() is False
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 11


class BlockingSource:
    def __init__(self, db):
        self.db = db
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.blocking = True

    async def get_settings(self, chat_id):
        settings = await self.db.get_settings(chat_id)
        if self.blocking:
            self.blocking = False
            self.reached.set()
            await self.release.wait()
        return settings

    async def migrated_from(self, chat_id):
        return await self.db.migrated_from(chat_id)


class TestConcurrentRuleChanges:
    async def test_simultaneous_addrule_keeps_every_pattern(self, services):
        patterns = [f"spam{i}" for i in range(6)]
        results = await asyncio.gather(
            *(
                services.moderation.add_rule(CHAT_ID, "text_pattern", pattern)
                for pattern in patterns
            )
        )
        assert results == [None] * 6
        rules = (await services.rules.get(CHAT_ID)).rules
        assert set(patterns) <= set(rules.detectors.text_pattern.patterns)

    async def test_simultaneous_delrule_removes_every_pattern(
        self, db, services
    ):
        document = {
            "defaults": {
                "detectors": {
                    "text_pattern": {"patterns": ["a", "b", "c", "d"]}
                }
            }
        }
        services.moderation.rules = RulesStore(
            db, parse_rules_document(document)
        )
        results = await asyncio.gather(
            *(
                services.moderation.del_rule(CHAT_ID, "text_pattern", pattern)
                for pattern in "abcd"
            )
        )
        assert results == [None] * 4
        rules = (await services.moderation.rules.get(CHAT_ID)).rules
        assert rules.detectors.text_pattern.patterns == []

    async def test_concurrent_thresholds_cannot_save_bad_order(self, services):
        await services.rules.get(CHAT_ID)
        results = await asyncio.gather(
            services.moderation.set_threshold(CHAT_ID, "restrict", 95),
            services.moderation.set_threshold(CHAT_ID, "ban", 80),
        )
        assert sum(result is not None for result in results) == 1
        ruleset = await services.rules.get(CHAT_ID)
        assert ruleset.skipped == ()


class TestAddAndDeleteRule:
    async def test_delrule_removes_pattern_in_both_lists(self, db):
        document = {
            "defaults": {"detectors": {"text_pattern": {"patterns": ["foo"]}}}
        }
        store = RulesStore(db, parse_rules_document(document))
        await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["foo"])
        moderation = ModerationService(None, db, store, None, None, None, None)
        assert await moderation.del_rule(CHAT_ID, "text_pattern", "foo") is None
        rules = (await store.get(CHAT_ID)).rules
        assert rules.detectors.text_pattern.patterns == []

    async def test_delrule_of_added_pattern_leaves_no_tombstone(self, db):
        store = RulesStore(db, parse_rules_document(TEST_RULES))
        moderation = ModerationService(None, db, store, None, None, None, None)
        await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["foo"])
        store.invalidate(CHAT_ID)
        assert await moderation.del_rule(CHAT_ID, "text_pattern", "foo") is None
        settings = await db.get_settings(CHAT_ID)
        assert "pattern_del.text_pattern" not in settings

    async def test_pattern_gone_from_file_is_actually_added(self, db):
        store = RulesStore(db, parse_rules_document(TEST_RULES))
        await db.set_setting(CHAT_ID, "pattern_del.text_pattern", ["foo"])
        moderation = ModerationService(None, db, store, None, None, None, None)
        assert await moderation.add_rule(CHAT_ID, "text_pattern", "foo") is None
        rules = (await store.get(CHAT_ID)).rules
        assert rules.detectors.text_pattern.patterns == ["foo"]

    async def test_pattern_still_in_file_is_just_restored(self, db):
        document = {
            "defaults": {"detectors": {"text_pattern": {"patterns": ["foo"]}}}
        }
        store = RulesStore(db, parse_rules_document(document))
        await db.set_setting(CHAT_ID, "pattern_del.text_pattern", ["foo"])
        moderation = ModerationService(None, db, store, None, None, None, None)
        assert await moderation.add_rule(CHAT_ID, "text_pattern", "foo") is None
        settings = await db.get_settings(CHAT_ID)
        assert settings["pattern_del.text_pattern"] == []
        assert "pattern_add.text_pattern" not in settings


class TestRulesCacheRaces:
    async def test_setting_changed_during_build_is_not_lost(self, db):
        source = BlockingSource(db)
        store = RulesStore(source, parse_rules_document(TEST_RULES))
        building = asyncio.create_task(store.get(CHAT_ID))
        await source.reached.wait()
        await db.set_setting(CHAT_ID, "enabled", False)
        store.invalidate(CHAT_ID)
        source.release.set()
        assert (await building).rules.enabled is False
        assert (await store.get(CHAT_ID)).rules.enabled is False

    async def test_reload_during_build_is_not_lost(self, db, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("defaults:\n  tiers:\n    delete: 11\n")
        source = BlockingSource(db)
        store = RulesStore(source, load_rules_file(path), str(path))
        building = asyncio.create_task(store.get(CHAT_ID))
        await source.reached.wait()
        path.write_text("defaults:\n  tiers:\n    delete: 22\n")
        assert await store.reload() is True
        source.release.set()
        await building
        assert (await store.get(CHAT_ID)).rules.tiers.delete == 22

    async def test_unrelated_build_still_caches(self, db):
        store = RulesStore(db, parse_rules_document(TEST_RULES))
        first = await store.get(CHAT_ID)
        assert await store.get(CHAT_ID) is first


class TestInvalidSettingIsSkippedAlone:
    FILE = {"defaults": {"tiers": {"delete": 90, "delete_and_restrict": 95}}}

    async def store_with_conflict(self, db):
        await db.set_setting(CHAT_ID, "enabled", False)
        await db.set_setting(CHAT_ID, "tiers.delete_and_ban", 80)
        await db.set_setting(CHAT_ID, "pattern_add.text_pattern", ["keep"])
        return RulesStore(db, parse_rules_document(self.FILE))

    async def test_only_the_invalid_group_is_dropped(self, db):
        ruleset = await (await self.store_with_conflict(db)).get(CHAT_ID)
        assert ruleset.rules.enabled is False
        assert ruleset.rules.detectors.text_pattern.patterns == ["keep"]
        assert ruleset.rules.tiers.delete == 90
        assert ruleset.rules.tiers.delete_and_ban == 100
        assert ruleset.skipped == ("tiers",)

    async def test_tier_changes_that_only_work_together_are_kept(self, db):
        await db.set_setting(CHAT_ID, "tiers.delete", 120)
        await db.set_setting(CHAT_ID, "tiers.delete_and_restrict", 130)
        await db.set_setting(CHAT_ID, "tiers.delete_and_ban", 150)
        ruleset = await RulesStore(db, parse_rules_document(TEST_RULES)).get(
            CHAT_ID
        )
        assert ruleset.rules.tiers.delete == 120
        assert ruleset.skipped == ()

    async def test_bad_duration_keeps_other_settings(self, db):
        await db.set_setting(CHAT_ID, "enabled", False)
        await db.set_setting(CHAT_ID, "restrict_duration", 3600)
        ruleset = await RulesStore(db, parse_rules_document(TEST_RULES)).get(
            CHAT_ID
        )
        assert ruleset.rules.enabled is False
        assert ruleset.rules.restrict_duration == 86400
        assert ruleset.skipped == ("restrict_duration",)

    async def test_status_shows_ignored_settings(self, db, services):
        await db.set_setting(CHAT_ID, "restrict_duration", 3600)
        services.rules.invalidate(CHAT_ID)
        card = await services.moderation.status_card(CHAT_ID)
        text = rich_text(card.model_dump(exclude_none=True))
        assert "Ignored settings" in text
        assert "restrict_duration" in text


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "value,expected",
        [("info", "INFO"), (" Debug ", "DEBUG"), ("WARNING", "WARNING")],
    )
    def test_log_level_is_normalised(
        self, monkeypatch, tmp_path, value, expected
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_LEVEL", value)
        settings = Settings(
            bot_token=SecretStr(FAKE_TOKEN),
            db_encryption_key=SecretStr(TEST_DB_KEY),
            allowed_chat_ids=frozenset({CHAT_ID}),
        )
        assert settings.log_level == expected

    def test_unknown_log_level_exits_cleanly(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BOT_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("DB_ENCRYPTION_KEY", TEST_DB_KEY)
        monkeypatch.setenv("ALLOWED_CHAT_IDS", str(CHAT_ID))
        monkeypatch.setenv("LOG_LEVEL", "loud")
        with pytest.raises(SystemExit) as caught:
            main_module.cli()
        assert caught.value.code == 2
        assert "log_level" in capsys.readouterr().err

    def test_non_utf8_rules_file_is_a_config_error(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_bytes(b"\xff\xfe bad")
        with pytest.raises(ConfigError, match="cannot read"):
            load_rules_file(path)
