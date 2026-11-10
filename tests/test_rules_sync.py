from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import SHIPPED_RULES
from tg_antispam_bot import main
from tg_antispam_bot.config import ConfigError, default_env_file
from tg_antispam_bot.rules_sync import (
    apply_sync,
    merge,
    plan_sync,
    snapshot_path,
)

BASE = """\
defaults:
  detectors:
    text_pattern:
      weight: 50
      patterns:
        - "free\\\\s+crypto"
        - "old\\\\s+scam"
"""
SHIPPED = """\
defaults:
  remove_guest_bots_summoned_by_members: true
  detectors:
    text_pattern:
      weight: 50
      patterns:
        - "free\\\\s+crypto"
        - "old\\\\s+scam"
        - "new\\\\s+scam"
    keyword_cluster:
      min_matches: 4
chats: {}
"""


def test_adds_new_keys_and_patterns_in_shipped_order():
    local = """\
# my rules
defaults:
  detectors:
    text_pattern:
      weight: 50  # tuned
      patterns:
        - "free\\\\s+crypto"
        - "old\\\\s+scam"
"""
    assert (
        merge(local, SHIPPED, BASE)
        == """\
# my rules
defaults:
  remove_guest_bots_summoned_by_members: true
  detectors:
    text_pattern:
      weight: 50  # tuned
      patterns:
        - "free\\\\s+crypto"
        - "old\\\\s+scam"
        - "new\\\\s+scam"
    keyword_cluster:
      min_matches: 4
"""
    )


def test_keeps_changed_values_and_removed_rules():
    local = """\
defaults:
  detectors:
    text_pattern:
      weight: 80
      patterns:
        - "free\\\\s+crypto"
"""
    merged = merge(local, SHIPPED, BASE)
    assert "weight: 80" in merged
    assert "old\\\\s+scam" not in merged
    assert "new\\\\s+scam" in merged


def test_keys_removed_since_the_last_sync_stay_removed():
    local = "defaults:\n  detectors: {}\n"
    base = "defaults:\n  detectors: {}\n  raid_mode: {auto: true}\n"
    shipped = base + "  enabled: true\n"
    assert merge(local, shipped, base) == (
        "defaults:\n  detectors: {}\n  enabled: true\n"
    )


def test_plan_shows_a_diff_and_apply_writes_it(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(BASE)
    snapshot_path(rules).write_text(BASE)
    result = plan_sync(rules, SHIPPED)
    assert '+        - "new\\\\s+scam"' in result.diff
    assert rules.read_text() == BASE
    apply_sync(rules, result)
    assert rules.read_text() == result.text
    assert snapshot_path(rules).read_text() == SHIPPED
    assert plan_sync(rules, SHIPPED).diff == ""


def test_up_to_date_file_is_left_untouched(tmp_path):
    rules = tmp_path / "rules.yaml"
    text = "defaults:\n  enabled: true\n"
    rules.write_text(text)
    result = plan_sync(rules, text)
    assert result.diff == ""
    apply_sync(rules, result)
    assert rules.read_text() == text
    assert snapshot_path(rules).read_text() == text


def test_invalid_result_is_refused(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("defaults:\n  tiers: {delete: 500}\n")
    with pytest.raises(ConfigError):
        plan_sync(rules, "defaults:\n  enabled: true\n")


def test_shipped_rules_sync_cleanly_onto_an_empty_file(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("")
    result = plan_sync(rules)
    assert result.text == SHIPPED_RULES.read_text().replace("chats: {}\n", "")


def test_rules_subcommand_prints_the_diff(monkeypatch, capsys, tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(BASE)
    snapshot_path(rules).write_text(BASE)
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg-antispam-bot", "rules", "sync", "--rules-path", str(rules)],
    )
    monkeypatch.setattr(
        "tg_antispam_bot.rules_sync.shipped_rules", lambda: SHIPPED
    )
    main.cli()
    output = capsys.readouterr().out
    assert "+  remove_guest_bots_summoned_by_members: true" in output
    assert rules.read_text() == BASE
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--apply"])
    main.cli()
    assert "remove_guest_bots_summoned_by_members" in rules.read_text()


def test_apply_keeps_the_rules_file_mode(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(BASE)
    rules.chmod(0o644)
    apply_sync(rules, plan_sync(rules, SHIPPED))
    assert rules.stat().st_mode & 0o777 == 0o644
    assert snapshot_path(rules).stat().st_mode & 0o777 == 0o640


def test_rules_path_comes_from_the_installed_env_file(
    monkeypatch, capsys, tmp_path
):
    config = tmp_path / "config"
    env_file = config / "tg-antispam-bot" / ".env"
    env_file.parent.mkdir(parents=True)
    rules = tmp_path / "my-rules.yaml"
    rules.write_text(BASE)
    env_file.write_text(f"RULES_PATH={rules}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("RULES_PATH", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert default_env_file() == env_file
    monkeypatch.setattr(sys, "argv", ["tg-antispam-bot", "rules", "sync"])
    monkeypatch.setattr(
        "tg_antispam_bot.rules_sync.shipped_rules", lambda: SHIPPED
    )
    main.cli()
    assert f"a/{rules.name}" in capsys.readouterr().out


def test_env_file_falls_back_to_the_current_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert default_env_file() == Path(".env")
