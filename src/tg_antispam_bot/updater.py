from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .config import UpdateSettings, default_env_file
from .files import copy_regular_file, write_atomic
from .rules_sync import snapshot_path

logger = logging.getLogger(__name__)

DISTRIBUTION = "tg-antispam-bot"
SERVICE = "tg-antispam-bot"
STATE_DIRECTORY = "tg-antispam-bot-update"
COMMAND_TIMEOUT_SECONDS = 600
GITHUB_URL = re.compile(
    r"^(?:https://|ssh://git@|git@)github\.com[/:]"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Installed:
    url: str
    commit: str
    version: str


@dataclass(frozen=True)
class Target:
    commit: str
    label: str


def run(command: list[str]) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise UpdateError(
            f"{shlex.join(command)} failed: {detail.strip()}"
        ) from exc
    return completed.stdout


def installed() -> Installed:
    distribution = metadata.distribution(DISTRIBUTION)
    raw = distribution.read_text("direct_url.json")
    info = json.loads(raw) if raw else {}
    vcs = info.get("vcs_info", {})
    if vcs.get("vcs") != "git" or not vcs.get("commit_id"):
        raise UpdateError(
            "the bot wasn't installed from git, reinstall it with install.sh"
        )
    return Installed(info["url"], vcs["commit_id"], distribution.version)


def _release_version(name: str) -> Version | None:
    try:
        version = Version(name.removeprefix("v"))
    except InvalidVersion:
        return None
    return None if version.is_prerelease else version


def tag_commits(refs: Mapping[str, str]) -> dict[str, str]:
    commits: dict[str, str] = {}
    for ref, commit in refs.items():
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            commits[name.removesuffix("^{}")] = commit
        else:
            commits.setdefault(name, commit)
    return commits


def newest_tag(refs: Mapping[str, str], current: str) -> Target | None:
    floor = Version(current)
    newer = [
        (version, name, commit)
        for name, commit in tag_commits(refs).items()
        if (version := _release_version(name)) is not None and version > floor
    ]
    if not newer:
        return None
    _, name, commit = max(newer)
    return Target(commit, name)


class Git:
    def __init__(self, cache: Path):
        self.cache = cache

    def ls_remote(self, url: str, *patterns: str) -> dict[str, str]:
        output = run(["git", "ls-remote", url, *patterns])
        refs: dict[str, str] = {}
        for line in output.splitlines():
            commit, _, ref = line.partition("\t")
            refs[ref] = commit
        return refs

    def is_fast_forward(
        self, url: str, branch: str, old: str, new: str
    ) -> bool:
        if not self.cache.exists():
            run(["git", "init", "--quiet", "--bare", str(self.cache)])
        run(
            [
                "git",
                "-C",
                str(self.cache),
                "fetch",
                "--quiet",
                "--no-tags",
                "--filter=tree:0",
                url,
                f"+refs/heads/{branch}:refs/heads/{branch}",
            ]
        )
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.cache), "merge-base", "--is-ancestor"]
            + [old, new],
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return result.returncode == 0


def latest_release(url: str) -> str | None:
    match = GITHUB_URL.match(url)
    if match is None:
        raise UpdateError("UPDATE_SOURCE=release needs a GitHub source")
    request = urllib.request.Request(  # noqa: S310
        f"https://api.github.com/repos/{match['owner']}/{match['repo']}"
        "/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "tg-antispam-bot-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)["tag_name"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub releases lookup failed: {exc}") from exc


def find_target(
    settings: UpdateSettings,
    current: Installed,
    git: Git,
    release_lookup: Callable[[str], str | None] | None = None,
) -> Target | None:
    source = settings.update_source
    if source == "branch":
        branch = settings.update_branch
        ref = f"refs/heads/{branch}"
        commit = git.ls_remote(current.url, ref).get(ref)
        if commit is None:
            raise UpdateError(f"branch {branch} doesn't exist")
        if commit == current.commit:
            return None
        if not git.is_fast_forward(current.url, branch, current.commit, commit):
            raise UpdateError(
                f"{branch} at {commit[:12]} doesn't fast-forward from the "
                f"installed {current.commit[:12]}, refusing to update"
            )
        return Target(commit, f"{branch}@{commit[:12]}")
    if source == "tag":
        refs = git.ls_remote(current.url, "refs/tags/*")
        return newest_tag(refs, current.version)
    tag = (release_lookup or latest_release)(current.url)
    if tag is None:
        return None
    refs = git.ls_remote(
        current.url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"
    )
    return newest_tag(refs, current.version)


def pip_install(url: str, commit: str) -> None:
    spec = f"{DISTRIBUTION} @ git+{url}@{commit}"
    run([sys.executable, "-m", "pip", "install", "--quiet", spec])


class Service:
    def __init__(self, user: bool):
        self.systemctl = ["systemctl", "--user"] if user else ["systemctl"]

    def stop(self) -> None:
        run([*self.systemctl, "stop", SERVICE])

    def start(self) -> None:
        run([*self.systemctl, "start", SERVICE])


def sync_rules_with_new_version(rules_path: Path) -> None:
    command = Path(sys.executable).with_name(DISTRIBUTION)
    run(
        [
            str(command),
            "rules",
            "sync",
            "--apply",
            "--rules-path",
            str(rules_path),
        ]
    )


class Updater:
    def __init__(
        self,
        settings: UpdateSettings,
        service: Service,
        git: Git,
        state_dir: Path,
        current: Callable[[], Installed] = installed,
        install: Callable[[str, str], None] = pip_install,
        sync_rules: Callable[[Path], None] = sync_rules_with_new_version,
    ):
        self.settings = settings
        self.service = service
        self.install = install
        self.git = git
        self.current = current
        self.sync_rules = sync_rules
        self.rules_path = Path(settings.rules_path)
        self.backup_dir = state_dir / "backup"
        self.state_file = state_dir / "state.json"

    def check(self) -> tuple[Installed, Target | None]:
        current = self.current()
        target = find_target(self.settings, current, self.git)
        if target is not None and target.commit in self._skipped():
            logger.info(
                "skipping update that was rolled back",
                extra={"target": target.label},
            )
            return current, None
        return current, target

    def update(self) -> Target | None:
        current, target = self.check()
        if target is None:
            return None
        rollback = self.settings.update_rollback
        logger.info(
            "updating bot",
            extra={"from_commit": current.commit, "target": target.label},
        )
        self.service.stop()
        if rollback:
            try:
                self._backup()
            except (UpdateError, OSError):
                self.service.start()
                raise
        try:
            self.install(current.url, target.commit)
            if self.settings.update_rules:
                self._sync_rules()
            self.service.start()
        except (UpdateError, OSError) as exc:
            outcome = "failed"
            if rollback:
                self._roll_back(current, target)
                outcome = "failed and was rolled back"
            else:
                with suppress(UpdateError):
                    self.service.start()
            raise UpdateError(
                f"update to {target.label} {outcome}: {exc}"
            ) from exc
        return target

    @property
    def _kept_files(self) -> list[Path]:
        db = Path(self.settings.db_path)
        return [
            db,
            db.with_name(db.name + "-wal"),
            self.rules_path,
            snapshot_path(self.rules_path),
        ]

    def _backup(self) -> None:
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        self.backup_dir.mkdir(mode=0o700)
        for index, path in enumerate(self._kept_files):
            copy_regular_file(path, self.backup_dir / str(index))

    def _restore(self) -> None:
        db = Path(self.settings.db_path)
        db.with_name(db.name + "-shm").unlink(missing_ok=True)
        for index, path in enumerate(self._kept_files):
            parent = path.parent.stat()
            owner = (parent.st_uid, parent.st_gid)
            saved = self.backup_dir / str(index)
            if not copy_regular_file(saved, path, owner):
                path.unlink(missing_ok=True)

    def _roll_back(self, current: Installed, target: Target) -> None:
        logger.error(
            "update failed, rolling back",
            extra={"to_commit": current.commit, "target": target.label},
        )
        with suppress(UpdateError):
            self.service.stop()
        try:
            self.install(current.url, current.commit)
        finally:
            self._restore()
            self._skip(target.commit)
            self.service.start()

    def _sync_rules(self) -> None:
        if snapshot_path(self.rules_path).exists():
            self.sync_rules(self.rules_path)
        else:
            logger.warning(
                "rules sync skipped until rules sync --apply runs once",
                extra={"rules_path": str(self.rules_path)},
            )

    def _skipped(self) -> list[str]:
        if not self.state_file.exists():
            return []
        return json.loads(self.state_file.read_text()).get("skipped", [])

    def _skip(self, commit: str) -> None:
        skipped = [*self._skipped(), commit]
        write_atomic(self.state_file, json.dumps({"skipped": skipped}))


def state_directory() -> Path:
    configured = os.environ.get("STATE_DIRECTORY")
    if configured:
        return Path(configured.split(":")[0])
    if os.geteuid() == 0:
        return Path("/var/lib") / STATE_DIRECTORY
    home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(home) / STATE_DIRECTORY


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise UpdateError(
            f"{path} must be a directory owned by you with mode 700"
        )
    return path


@contextmanager
def update_lock(state_dir: Path) -> Iterator[None]:
    descriptor = os.open(
        private_directory(state_dir) / "update.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another update is already running") from exc
        yield


def update_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="tg-antispam-bot update")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether an update is available",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="do nothing unless AUTO_UPDATE=true, for the update timer",
    )
    parser.add_argument(
        "--env-file", type=Path, help="defaults to the installer's .env"
    )
    args = parser.parse_args(argv)
    env_file = args.env_file or default_env_file()
    settings = UpdateSettings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    if args.auto and not settings.auto_update:
        print(
            "Automatic updates are off. Set AUTO_UPDATE=true to turn them on."
        )
        return
    state_dir = state_directory()
    updater = Updater(
        settings,
        Service(user=os.geteuid() != 0),
        Git(state_dir / "source.git"),
        state_dir,
    )
    try:
        with update_lock(state_dir):
            if args.check:
                _, target = updater.check()
                found = "Update available: {}."
            else:
                target = updater.update()
                found = "Updated to {}."
    except (UpdateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Up to date." if target is None else found.format(target.label))
