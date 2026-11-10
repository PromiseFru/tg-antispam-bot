from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tg_antispam_bot import main, updater
from tg_antispam_bot.config import UpdateSettings
from tg_antispam_bot.rules_sync import snapshot_path
from tg_antispam_bot.updater import (
    Git,
    Installed,
    Service,
    Target,
    UpdateError,
    Updater,
    find_target,
    latest_release,
    newest_tag,
    private_directory,
    update_lock,
)

GIT_CONFIG = {
    "user.email": "bot@example.com",
    "user.name": "Bot",
    "commit.gpgsign": "false",
    "tag.gpgsign": "false",
    "init.defaultBranch": "main",
}


@pytest.fixture(autouse=True)
def no_installed_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    for index, (key, value) in enumerate(GIT_CONFIG.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(GIT_CONFIG)))
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    commit(path, "first")
    return path


def settings(**values) -> UpdateSettings:
    return UpdateSettings.model_validate(values)


def installed_at(repo: Path, commit_id: str) -> Installed:
    return Installed(f"file://{repo}", commit_id, "0.1.0")


class TestNewestTag:
    def test_picks_highest_release_above_current(self):
        refs = {
            "refs/tags/v0.1.0": "a",
            "refs/tags/v0.3.0": "tag-object",
            "refs/tags/v0.3.0^{}": "c",
            "refs/tags/v0.2.0": "b",
            "refs/tags/v0.4.0rc1": "d",
            "refs/tags/latest": "e",
        }
        assert newest_tag(refs, "0.1.0") == Target("c", "v0.3.0")

    @pytest.mark.parametrize("installed", ["0.2.0", "0.2.1.dev3+g1234567"])
    def test_installed_or_older_tag_is_not_an_update(self, installed):
        assert newest_tag({"refs/tags/v0.2.0": "a"}, installed) is None


class TestFindTarget:
    def test_branch_at_the_installed_commit_is_not_an_update(
        self, repo, tmp_path
    ):
        target = find_target(
            settings(update_source="branch"),
            installed_at(repo, git(repo, "rev-parse", "HEAD")),
            Git(tmp_path / "cache.git"),
        )
        assert target is None

    def test_branch_fast_forward(self, repo, tmp_path):
        old = git(repo, "rev-parse", "HEAD")
        new = commit(repo, "second")
        target = find_target(
            settings(update_source="branch"),
            installed_at(repo, old),
            Git(tmp_path / "cache.git"),
        )
        assert target == Target(new, f"main@{new[:12]}")

    def test_branch_rewrite_is_refused(self, repo, tmp_path):
        installed_commit = commit(repo, "second")
        git(repo, "reset", "--quiet", "--hard", "HEAD~1")
        commit(repo, "rewritten")
        with pytest.raises(UpdateError, match="doesn't fast-forward"):
            find_target(
                settings(update_source="branch"),
                installed_at(repo, installed_commit),
                Git(tmp_path / "cache.git"),
            )

    def test_tag_uses_the_tagged_commit(self, repo, tmp_path):
        old = git(repo, "rev-parse", "HEAD")
        new = commit(repo, "release")
        git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        commit(repo, "after release")
        target = find_target(
            settings(update_source="tag"),
            installed_at(repo, old),
            Git(tmp_path / "cache.git"),
        )
        assert target == Target(new, "v0.2.0")

    def test_release_uses_the_latest_release_tag(self, repo, tmp_path):
        old = git(repo, "rev-parse", "HEAD")
        released = commit(repo, "release")
        git(repo, "tag", "v0.2.0")
        commit(repo, "unreleased")
        git(repo, "tag", "v0.3.0")
        target = find_target(
            settings(update_source="release"),
            installed_at(repo, old),
            Git(tmp_path / "cache.git"),
            release_lookup=lambda url: "v0.2.0",
        )
        assert target == Target(released, "v0.2.0")

    @pytest.mark.parametrize("release", [None, "v0.1.0"])
    def test_no_newer_release_means_no_update(self, repo, tmp_path, release):
        target = find_target(
            settings(update_source="release"),
            installed_at(repo, git(repo, "rev-parse", "HEAD")),
            Git(tmp_path / "cache.git"),
            release_lookup=lambda url: release,
        )
        assert target is None


def test_release_source_needs_github():
    with pytest.raises(UpdateError, match="GitHub"):
        latest_release("https://gitlab.com/owner/repo")


class FakeGit(Git):
    def __init__(self):
        super().__init__(Path("unused"))

    def ls_remote(self, url, *patterns):
        return {"refs/heads/main": "new"}

    def is_fast_forward(self, url, branch, old, new):
        return True


class FakeService(Service):
    def __init__(self, healthy: bool):
        super().__init__(user=True)
        self.calls: list[str] = []
        self.fail_next_start = not healthy

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")
        if self.fail_next_start and self.calls.count("start") == 1:
            raise UpdateError("start failed")


class Deployment:
    def __init__(self, root: Path):
        self.db = root / "bot.db"
        self.db.write_text("original data")
        self.rules = root / "rules.yaml"
        self.rules.write_text("defaults: {}\n")
        snapshot_path(self.rules).write_text("old shipped\n")
        self.state = private_directory(root / "updates")
        self.installs: list[str] = []

    def install(self, url: str, commit: str) -> None:
        self.installs.append(commit)
        self.db.write_text(f"migrated by {commit}")

    def updater(self, healthy=True, **values) -> Updater:
        def sync(path: Path) -> None:
            path.write_text("synced\n")
            snapshot_path(path).write_text("shipped\n")

        return Updater(
            settings(
                update_source="branch",
                db_path=str(self.db),
                rules_path=str(self.rules),
                **values,
            ),
            FakeService(healthy),
            FakeGit(),
            self.state,
            current=lambda: Installed("file:///repo", "old", "0.1.0"),
            install=self.install,
            sync_rules=sync,
        )


@pytest.fixture
def deployment(tmp_path) -> Deployment:
    return Deployment(tmp_path)


class TestUpdater:
    def test_update_installs_syncs_rules_and_restarts(self, deployment):
        built = deployment.updater()
        assert built.update() == Target("new", "main@new")
        assert deployment.installs == ["new"]
        assert deployment.rules.read_text() == "synced\n"
        assert built.service.calls == ["stop", "start"]

    def test_rules_sync_waits_for_a_first_manual_sync(self, deployment):
        snapshot_path(deployment.rules).unlink()
        deployment.updater().update()
        assert deployment.rules.read_text() == "defaults: {}\n"

    def test_rules_sync_can_be_turned_off(self, deployment):
        deployment.updater(update_rules=False).update()
        assert deployment.rules.read_text() == "defaults: {}\n"

    def test_failed_start_rolls_back_and_skips_the_version(self, deployment):
        built = deployment.updater(healthy=False)
        with pytest.raises(UpdateError, match="rolled back"):
            built.update()
        assert deployment.installs == ["new", "old"]
        assert deployment.db.read_text() == "original data"
        assert deployment.rules.read_text() == "defaults: {}\n"
        assert snapshot_path(deployment.rules).read_text() == "old shipped\n"
        assert built.service.calls == ["stop", "start", "stop", "start"]
        assert built.update() is None
        assert deployment.installs == ["new", "old"]

    def test_symlinked_database_is_refused_before_updating(
        self, deployment, tmp_path
    ):
        victim = tmp_path / "victim"
        victim.write_text("secret")
        deployment.db.unlink()
        deployment.db.symlink_to(victim)
        built = deployment.updater()
        with pytest.raises(OSError, match="isn't a regular file"):
            built.update()
        assert deployment.installs == []
        assert built.service.calls == ["stop", "start"]

    def test_hard_linked_database_is_refused(self, deployment, tmp_path):
        (tmp_path / "other").hardlink_to(deployment.db)
        with pytest.raises(OSError, match="isn't a regular file"):
            deployment.updater().update()
        assert deployment.installs == []

    def test_rollback_replaces_planted_symlinks(self, deployment, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("secret")
        wal = deployment.db.with_name("bot.db-wal")
        built = deployment.updater(healthy=False)

        def plant(url, commit):
            deployment.installs.append(commit)
            if commit == "new":
                deployment.db.unlink()
                deployment.db.symlink_to(victim)
                wal.symlink_to(victim)

        built.install = plant
        with pytest.raises(UpdateError, match="rolled back"):
            built.update()
        assert victim.read_text() == "secret"
        assert not deployment.db.is_symlink()
        assert deployment.db.read_text() == "original data"
        assert not wal.exists() and not wal.is_symlink()

    def test_rollback_can_be_turned_off(self, deployment):
        built = deployment.updater(healthy=False, update_rollback=False)
        with pytest.raises(UpdateError) as raised:
            built.update()
        assert "rolled back" not in str(raised.value)
        assert deployment.installs == ["new"]
        assert deployment.db.read_text() == "migrated by new"
        assert built.service.calls == ["stop", "start", "start"]

    def test_failed_reinstall_still_restores_data(self, deployment):
        built = deployment.updater(healthy=False)

        def install(url, commit):
            deployment.install(url, commit)
            if commit == "old":
                raise UpdateError("pip failed")

        built.install = install
        with pytest.raises(UpdateError, match="pip failed"):
            built.update()
        assert deployment.db.read_text() == "original data"
        assert built.service.calls[-1] == "start"
        assert built.update() is None


def test_lock_never_follows_a_symlink(tmp_path):
    state = private_directory(tmp_path / "updates")
    victim = tmp_path / "victim"
    victim.write_text("secret")
    (state / "update.lock").symlink_to(victim)
    with pytest.raises(OSError):
        with update_lock(state):
            pass
    assert victim.read_text() == "secret"


@pytest.mark.parametrize("mode", [0o750, 0o707])
def test_state_directory_must_be_private(tmp_path, mode):
    state = tmp_path / "updates"
    state.mkdir()
    state.chmod(mode)
    with pytest.raises(UpdateError, match="mode 700"):
        private_directory(state)


def test_symlinked_state_directory_is_refused(tmp_path):
    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "updates").symlink_to(tmp_path / "real")
    with pytest.raises(UpdateError, match="mode 700"):
        private_directory(tmp_path / "updates")


def test_second_update_is_refused_while_one_runs(tmp_path):
    with update_lock(tmp_path):
        with pytest.raises(UpdateError, match="already running"):
            with update_lock(tmp_path):
                pass


class FakeDistribution:
    version = "0.1.0"

    def __init__(self, direct_url: dict):
        self.direct_url = direct_url

    def read_text(self, name):
        return json.dumps(self.direct_url)


def test_installed_reads_the_git_commit(monkeypatch):
    info = {
        "url": "https://github.com/owner/repo",
        "vcs_info": {"vcs": "git", "commit_id": "abc"},
    }
    monkeypatch.setattr(
        updater.metadata, "distribution", lambda name: FakeDistribution(info)
    )
    assert updater.installed() == Installed(
        "https://github.com/owner/repo", "abc", "0.1.0"
    )


def test_installed_rejects_non_git_installs(monkeypatch):
    info = {"url": "file:///src", "dir_info": {"editable": True}}
    monkeypatch.setattr(
        updater.metadata, "distribution", lambda name: FakeDistribution(info)
    )
    with pytest.raises(UpdateError, match="install.sh"):
        updater.installed()


def test_timer_run_does_nothing_while_auto_update_is_off(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTO_UPDATE", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "updates"))
    monkeypatch.setattr(sys, "argv", ["tg-antispam-bot", "update", "--auto"])
    main.cli()
    assert "Automatic updates are off" in capsys.readouterr().out
    assert not (tmp_path / "updates").exists()


def test_check_reports_an_available_update(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "updates"))
    monkeypatch.setattr(
        Updater, "check", lambda self: (None, Target("abc", "v0.2.0"))
    )
    monkeypatch.setattr(sys, "argv", ["tg-antispam-bot", "update", "--check"])
    main.cli()
    assert capsys.readouterr().out == "Update available: v0.2.0.\n"


def test_update_reads_the_given_env_file(monkeypatch, capsys, tmp_path):
    env_file = tmp_path / "bot.env"
    env_file.write_text("AUTO_UPDATE=true\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTO_UPDATE", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "updates"))
    monkeypatch.setattr(Updater, "update", lambda self: Target("abc", "v0.2.0"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg-antispam-bot", "update", "--auto", "--env-file", str(env_file)],
    )
    main.cli()
    assert capsys.readouterr().out == "Updated to v0.2.0.\n"
