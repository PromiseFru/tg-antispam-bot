from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import (
    PATTERN_DETECTORS,
    ChatRules,
    ConfigError,
    RulesFile,
    load_rules_file,
    parse_chat_rules,
)
from .detectors import build_detectors
from .detectors.base import Detector
from .pattern_check import check_file_patterns

logger = logging.getLogger(__name__)

PATTERN_ADD_PREFIX = "pattern_add."
PATTERN_DEL_PREFIX = "pattern_del."
REJECTED_OVERRIDE = (ConfigError, KeyError, AttributeError, TypeError)


class SettingsSource(Protocol):
    async def get_settings(self, chat_id: int) -> dict[str, Any]: ...

    async def migrated_from(self, chat_id: int) -> int | None: ...


@dataclass(frozen=True)
class RuleSet:
    rules: ChatRules
    detectors: Sequence[Detector[Any]]
    skipped: tuple[str, ...] = ()


def apply_overrides(
    document: dict[str, Any], base: ChatRules, overrides: Mapping[str, Any]
) -> dict[str, Any]:
    for key, value in overrides.items():
        if key.startswith((PATTERN_ADD_PREFIX, PATTERN_DEL_PREFIX)):
            continue
        cursor = document
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    for name in PATTERN_DETECTORS:
        added = overrides.get(f"{PATTERN_ADD_PREFIX}{name}", [])
        removed = set(overrides.get(f"{PATTERN_DEL_PREFIX}{name}", []))
        if not added and not removed:
            continue
        current = getattr(base.detectors, name).patterns
        merged = [item for item in current if item not in removed]
        merged.extend(item for item in added if item not in merged)
        detectors = document.setdefault("detectors", {})
        detectors.setdefault(name, {})["patterns"] = merged
    return document


def _override_group(key: str) -> str:
    if key.startswith("tiers."):
        return "tiers"
    for prefix in (PATTERN_ADD_PREFIX, PATTERN_DEL_PREFIX):
        if key.startswith(prefix):
            return f"patterns.{key[len(prefix) :]}"
    return key


def _parse_with(
    chat_id: int,
    document: dict[str, Any],
    base: ChatRules,
    overrides: Mapping[str, Any],
) -> ChatRules:
    merged = apply_overrides(copy.deepcopy(document), base, overrides)
    return parse_chat_rules(merged, f"chat {chat_id}")


def merge_overrides(
    chat_id: int, document: dict[str, Any], overrides: Mapping[str, Any]
) -> tuple[ChatRules, tuple[str, ...]]:
    base = parse_chat_rules(document, f"chat {chat_id}")
    try:
        return _parse_with(chat_id, document, base, overrides), ()
    except REJECTED_OVERRIDE:
        pass
    groups: dict[str, dict[str, Any]] = {}
    for key in sorted(overrides):
        groups.setdefault(_override_group(key), {})[key] = overrides[key]
    accepted: dict[str, Any] = {}
    skipped: list[str] = []
    for group, values in groups.items():
        candidate = {**accepted, **values}
        try:
            _parse_with(chat_id, document, base, candidate)
        except REJECTED_OVERRIDE as exc:
            skipped.append(group)
            logger.error(
                "group setting ignored",
                extra={"chat_id": chat_id, "setting": group, "error": str(exc)},
            )
        else:
            accepted = candidate
    return _parse_with(chat_id, document, base, accepted), tuple(skipped)


class RulesStore:
    def __init__(
        self,
        source: SettingsSource,
        rules_file: RulesFile,
        path: str | None = None,
    ):
        self._source = source
        self._file = rules_file
        self._path = path
        self._cache: dict[int, RuleSet] = {}
        self._generation = 0
        self._reload_lock = asyncio.Lock()
        self._reloads: set[asyncio.Task[bool]] = set()

    @property
    def global_rules(self):
        return self._file.global_rules

    async def _file_document(self, chat_id: int) -> dict[str, Any]:
        file_chat_id = chat_id
        if chat_id not in self._file.chats:
            origin = await self._source.migrated_from(chat_id)
            file_chat_id = chat_id if origin is None else origin
        return self._file.base_document(file_chat_id)

    async def file_rules(self, chat_id: int) -> ChatRules:
        document = await self._file_document(chat_id)
        return parse_chat_rules(document, f"chat {chat_id}")

    async def _build(self, chat_id: int) -> RuleSet:
        document = await self._file_document(chat_id)
        overrides = await self._source.get_settings(chat_id)
        rules, skipped = merge_overrides(chat_id, document, overrides)
        detectors = await asyncio.to_thread(build_detectors, rules)
        return RuleSet(rules=rules, detectors=detectors, skipped=skipped)

    async def get(self, chat_id: int) -> RuleSet:
        while True:
            cached = self._cache.get(chat_id)
            if cached is not None:
                return cached
            generation = self._generation
            ruleset = await self._build(chat_id)
            if generation == self._generation:
                self._cache[chat_id] = ruleset
                return ruleset

    def invalidate(self, chat_id: int) -> None:
        self._generation += 1
        self._cache.pop(chat_id, None)

    def request_reload(self) -> None:
        task = asyncio.create_task(self.reload())
        self._reloads.add(task)
        task.add_done_callback(self._reloads.discard)

    async def reload(self) -> bool:
        if self._path is None:
            return False
        async with self._reload_lock:
            try:
                rules_file = load_rules_file(self._path)
                await check_file_patterns(rules_file)
            except ConfigError as exc:
                logger.error(
                    "rules reload failed, keeping current rules",
                    extra={"error": str(exc)},
                )
                return False
            self._file = rules_file
            self._generation += 1
            self._cache.clear()
        logger.info("rules reloaded", extra={"path": self._path})
        return True
