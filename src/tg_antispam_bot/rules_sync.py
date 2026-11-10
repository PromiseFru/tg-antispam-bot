from __future__ import annotations

import argparse
import copy
import difflib
import io
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from .config import (
    ConfigError,
    PathSettings,
    default_env_file,
    parse_rules_text,
)
from .files import write_atomic

SYNCED_SECTIONS = ("global", "defaults")
SNAPSHOT_SUFFIX = ".shipped"
RULES_MODE = 0o640


@dataclass(frozen=True)
class SyncResult:
    diff: str
    text: str
    shipped: str


def _yaml() -> YAML:
    handler = YAML()
    handler.preserve_quotes = True
    handler.width = 4096
    handler.indent(mapping=2, sequence=4, offset=2)
    return handler


def shipped_rules() -> str:
    return (
        resources.files("tg_antispam_bot")
        .joinpath("rules.yaml")
        .read_text(encoding="utf-8")
    )


def snapshot_path(rules_path: Path) -> Path:
    return rules_path.with_name(rules_path.name + SNAPSHOT_SUFFIX)


def write_snapshot(rules_path: Path, shipped: str) -> None:
    write_atomic(snapshot_path(rules_path), shipped, RULES_MODE)


def _load(handler: YAML, text: str) -> Any:
    document = handler.load(text)
    return document if document is not None else CommentedMap()


def _insert_after(
    target: CommentedMap, key: Any, value: Any, order: list[Any]
) -> None:
    position = 0
    for earlier in reversed(order[: order.index(key)]):
        if earlier in target:
            position = list(target).index(earlier) + 1
            break
    target.insert(position, key, copy.deepcopy(value))


def _add_new(local: Any, shipped: Any, base: Any) -> bool:
    changed = False
    if isinstance(local, CommentedMap) and isinstance(shipped, Mapping):
        base = base if isinstance(base, Mapping) else {}
        order = list(shipped)
        for key, value in shipped.items():
            if key in local:
                changed |= _add_new(local[key], value, base.get(key))
            elif key not in base:
                _insert_after(local, key, value, order)
                changed = True
    elif isinstance(local, list) and isinstance(shipped, list):
        base = base if isinstance(base, list) else []
        for item in shipped:
            if item not in local and item not in base:
                local.append(copy.deepcopy(item))
                changed = True
    return changed


def merge(local_text: str, shipped_text: str, base_text: str) -> str:
    if shipped_text == base_text:
        return local_text
    handler = _yaml()
    local = _load(handler, local_text)
    shipped = _load(handler, shipped_text)
    synced = CommentedMap(
        (key, value) for key, value in shipped.items() if key in SYNCED_SECTIONS
    )
    if not _add_new(local, synced, _load(handler, base_text)):
        return local_text
    output = io.StringIO()
    handler.dump(local, output)
    return output.getvalue()


def plan_sync(rules_path: Path, shipped: str | None = None) -> SyncResult:
    shipped = shipped_rules() if shipped is None else shipped
    local = rules_path.read_text(encoding="utf-8")
    snapshot = snapshot_path(rules_path)
    base = snapshot.read_text(encoding="utf-8") if snapshot.exists() else ""
    merged = merge(local, shipped, base)
    if merged != local:
        parse_rules_text(merged, str(rules_path))
    diff = "".join(
        difflib.unified_diff(
            local.splitlines(keepends=True),
            merged.splitlines(keepends=True),
            fromfile=f"a/{rules_path.name}",
            tofile=f"b/{rules_path.name}",
        )
    )
    return SyncResult(diff=diff, text=merged, shipped=shipped)


def apply_sync(rules_path: Path, result: SyncResult) -> None:
    if result.diff:
        write_atomic(rules_path, result.text, RULES_MODE)
    write_snapshot(rules_path, result.shipped)


def rules_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="tg-antispam-bot rules")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser(
        "sync", help="add new shipped rules to your rules file"
    )
    sync.add_argument(
        "--env-file", type=Path, help="defaults to the installer's .env"
    )
    sync.add_argument("--rules-path", type=Path, help="defaults to RULES_PATH")
    sync.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)
    env_file = args.env_file or default_env_file()
    rules_path = args.rules_path or Path(
        PathSettings(_env_file=env_file).rules_path  # pyright: ignore[reportCallIssue]
    )
    try:
        result = plan_sync(rules_path)
    except (OSError, ConfigError, YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not result.diff:
        print(f"No new shipped rules to add to {rules_path}.")
    else:
        sys.stdout.write(result.diff)
    if args.apply:
        try:
            apply_sync(rules_path, result)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if result.diff:
            print(
                f"Updated {rules_path}. Reload the bot to use it, "
                "or restart it if global changed."
            )
    elif result.diff:
        print("Run again with --apply to write these changes.")
