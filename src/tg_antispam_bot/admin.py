from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberOwner,
    InlineKeyboardMarkup,
    InputRichMessage,
)
from cachetools import TTLCache
from pydantic import ValidationError

from .config import MAX_CHAT_PATTERNS, PATTERN_DETECTORS, Tiers
from .db import ACTION_BAN, ACTION_RAID_RESTRICT, ACTION_RESTRICT, chat_label
from .pattern_check import check_user_pattern
from .render import (
    DOT_GRAY,
    DOT_GREEN,
    DOT_RED,
    VERB_CONFIRM,
    VERB_FALSE_POSITIVE,
    VERB_TRUST,
    AdminRow,
    DetectorRow,
    log_card,
    log_keyboard,
    log_size,
    status_card,
)
from .rules_store import PATTERN_ADD_PREFIX, PATTERN_DEL_PREFIX

if TYPE_CHECKING:
    from .db import ActionRecord

logger = logging.getLogger(__name__)

STALE_MULTIPLIER = 10
LINKED_CHAT_TTL = 3600
LINKED_CHAT_CACHE_SIZE = 4096

ChatAdmin = ChatMemberOwner | ChatMemberAdministrator


class AdminLookupError(Exception):
    pass


@dataclass
class _Entry:
    admins: dict[int, ChatAdmin]
    fetched_at: float


class AdminCache:
    def __init__(
        self,
        bot: Bot,
        ttl_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bot = bot
        self.ttl = ttl_seconds
        self.clock = clock
        self._entries: dict[int, _Entry] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._linked: TTLCache = TTLCache(
            maxsize=LINKED_CHAT_CACHE_SIZE, ttl=LINKED_CHAT_TTL, timer=clock
        )

    def invalidate(self, chat_id: int) -> None:
        self._entries.pop(chat_id, None)

    def forget(self, chat_id: int) -> None:
        self._entries.pop(chat_id, None)
        self._locks.pop(chat_id, None)
        self._linked.pop(chat_id, None)

    async def get_admins(
        self, chat_id: int, fresh: bool = False
    ) -> dict[int, ChatAdmin]:
        entry = self._entries.get(chat_id)
        if (
            not fresh
            and entry is not None
            and self.clock() - entry.fetched_at < self.ttl
        ):
            return entry.admins
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        requested_at = self.clock()
        async with lock:
            entry = self._entries.get(chat_id)
            if entry is not None:
                if fresh and entry.fetched_at >= requested_at:
                    return entry.admins
                if not fresh and self.clock() - entry.fetched_at < self.ttl:
                    return entry.admins
            try:
                members = await self.bot.get_chat_administrators(
                    chat_id=chat_id
                )
            except TelegramAPIError as exc:
                stale_limit = self.ttl * STALE_MULTIPLIER
                if (
                    not fresh
                    and entry is not None
                    and self.clock() - entry.fetched_at < stale_limit
                ):
                    logger.warning(
                        "using stale admin list",
                        extra={"chat_id": chat_id, "error": str(exc)},
                    )
                    return entry.admins
                raise AdminLookupError(str(exc)) from exc
            admins = {
                member.user.id: member
                for member in members
                if isinstance(
                    member, (ChatMemberOwner, ChatMemberAdministrator)
                )
            }
            self._entries[chat_id] = _Entry(
                admins=admins, fetched_at=self.clock()
            )
            return admins

    async def is_admin(
        self, chat_id: int, user_id: int, fresh: bool = False
    ) -> bool:
        return user_id in await self.get_admins(chat_id, fresh=fresh)

    async def linked_chat_id(self, chat_id: int) -> int | None:
        if chat_id in self._linked:
            return self._linked[chat_id]
        try:
            chat = await self.bot.get_chat(chat_id=chat_id)
        except TelegramAPIError as exc:
            raise AdminLookupError(str(exc)) from exc
        self._linked[chat_id] = chat.linked_chat_id
        return chat.linked_chat_id


TIER_ALIASES = {
    "delete": "delete",
    "restrict": "delete_and_restrict",
    "delete_and_restrict": "delete_and_restrict",
    "ban": "delete_and_ban",
    "delete_and_ban": "delete_and_ban",
}

PATTERN_CHECK_CONCURRENCY = 2
STATE_SUBSCRIBED = f"{DOT_GREEN} Subscribed"
STATE_IDLE = f"{DOT_GRAY} Not subscribed"
STATE_BLOCKED = f"{DOT_RED} Blocked the bot"
STATE_NOT_STARTED = f"{DOT_GRAY} Not started the bot"
STATUS_OK = "ok"
STATUS_ALREADY = "already"
STATUS_MISSING = "missing"
STATUS_FORBIDDEN = "forbidden"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ResolveOutcome:
    status: str
    action: ActionRecord | None = None
    detail: str = ""


class ModerationService:
    def __init__(self, bot, db, rules, admins, engine, executor, notify):
        self.bot = bot
        self.db = db
        self.rules = rules
        self.admins = admins
        self.engine = engine
        self.executor = executor
        self.notify = notify
        self._rule_locks: dict[int, asyncio.Lock] = {}
        self._pattern_checks = asyncio.Semaphore(PATTERN_CHECK_CONCURRENCY)

    def _rule_lock(self, chat_id: int) -> asyncio.Lock:
        return self._rule_locks.setdefault(chat_id, asyncio.Lock())

    async def resolve(
        self,
        action_id: int,
        verb: str,
        resolver_id: int,
        resolver_name: str,
        skip_dm: tuple[int, int] | None = None,
    ) -> ResolveOutcome:
        action = await self.db.get_action(action_id)
        if action is None:
            return ResolveOutcome(STATUS_MISSING)
        try:
            if not await self.admins.is_admin(
                action.chat_id, resolver_id, fresh=True
            ):
                return ResolveOutcome(STATUS_FORBIDDEN, action)
        except AdminLookupError:
            return ResolveOutcome(STATUS_UNAVAILABLE, action)
        lift_error = ""
        now = self.engine.now()
        claimed = await self.db.claim_resolution(
            action_id, resolver_id, resolver_name, verb, now
        )
        if not claimed:
            return ResolveOutcome(
                STATUS_ALREADY, await self.db.get_action(action_id)
            )
        related = await self._claim_related(
            action, resolver_id, resolver_name, verb, now
        )
        penalty = (
            None
            if verb == VERB_CONFIRM
            else _penalty_in_force([action, *related], now)
        )
        try:
            if penalty is not None:
                await self.executor.lift(
                    action.chat_id, action.user_id, penalty.action
                )
        except TelegramAPIError as exc:
            logger.warning(
                "could not lift sanction",
                extra={
                    "action_id": action_id,
                    "chat_id": action.chat_id,
                    "error": str(exc),
                },
            )
            if verb not in (VERB_TRUST, VERB_FALSE_POSITIVE):
                for claimed_action in (action, *related):
                    await self.db.release_resolution(claimed_action.id)
                return ResolveOutcome(STATUS_FAILED, action, str(exc))
            lift_error = str(exc)
        if verb != VERB_CONFIRM:
            self.engine.clear_sanction(action.chat_id, action.user_id)
        if verb == VERB_TRUST:
            await self.db.add_trusted(action.chat_id, action.user_id)
        elif verb == VERB_FALSE_POSITIVE:
            await self.db.clear_member_history(action.chat_id, action.user_id)
        updated = await self.db.get_action(action_id)
        logger.info(
            "action resolved",
            extra={
                "action_id": action_id,
                "verb": verb,
                "resolver": resolver_id,
            },
        )
        await self.notify.notify_resolved(updated, skip=skip_dm)
        for sibling in await self.db.get_actions([a.id for a in related]):
            await self.notify.notify_resolved(sibling, skip=skip_dm)
        return ResolveOutcome(STATUS_OK, updated, lift_error)

    async def _claim_related(
        self,
        action: ActionRecord,
        resolver_id: int,
        resolver_name: str,
        verb: str,
        now: int,
    ) -> list[ActionRecord]:
        related = []
        for other in await self.db.open_actions(action.chat_id, action.user_id):
            if await self.db.claim_resolution(
                other.id, resolver_id, resolver_name, verb, now
            ):
                related.append(other)
        return related

    async def set_threshold(
        self, chat_id: int, tier: str, value: int
    ) -> str | None:
        key = TIER_ALIASES.get(tier.lower())
        if key is None:
            return "Tier must be delete, restrict or ban."
        if value <= 0:
            return "The value must be a positive integer."
        async with self._rule_lock(chat_id):
            tiers = (await self.rules.get(chat_id)).rules.tiers
            try:
                Tiers.model_validate({**tiers.model_dump(), key: value})
            except ValidationError:
                return (
                    "Thresholds must satisfy delete ≤ restrict ≤ ban. "
                    "Current values: "
                    f"delete {tiers.delete}, "
                    f"restrict {tiers.delete_and_restrict}, "
                    f"ban {tiers.delete_and_ban}."
                )
            await self.db.set_setting(chat_id, f"tiers.{key}", value)
            self.rules.invalidate(chat_id)
            return None

    async def add_rule(
        self, chat_id: int, detector: str, pattern: str
    ) -> str | None:
        if detector not in PATTERN_DETECTORS:
            return f"Detector must be one of: {', '.join(PATTERN_DETECTORS)}."
        async with self._pattern_checks:
            problem = await check_user_pattern(pattern)
        if problem:
            return problem
        async with self._rule_lock(chat_id):
            settings = await self.db.get_settings(chat_id)
            added = list(settings.get(f"{PATTERN_ADD_PREFIX}{detector}", []))
            removed = list(settings.get(f"{PATTERN_DEL_PREFIX}{detector}", []))
            current = getattr(
                (await self.rules.get(chat_id)).rules.detectors, detector
            ).patterns
            if pattern in current:
                return "That pattern is already active."
            if len(current) >= MAX_CHAT_PATTERNS:
                return (
                    f"A detector can hold at most {MAX_CHAT_PATTERNS} patterns."
                )
            if pattern in removed:
                removed.remove(pattern)
                await self.db.set_setting(
                    chat_id, f"{PATTERN_DEL_PREFIX}{detector}", removed
                )
            file_rules = await self.rules.file_rules(chat_id)
            if pattern not in getattr(file_rules.detectors, detector).patterns:
                added.append(pattern)
                await self.db.set_setting(
                    chat_id, f"{PATTERN_ADD_PREFIX}{detector}", added
                )
            self.rules.invalidate(chat_id)
            return None

    async def del_rule(
        self, chat_id: int, detector: str, pattern: str
    ) -> str | None:
        if detector not in PATTERN_DETECTORS:
            return f"Detector must be one of: {', '.join(PATTERN_DETECTORS)}."
        async with self._rule_lock(chat_id):
            current = getattr(
                (await self.rules.get(chat_id)).rules.detectors, detector
            ).patterns
            if pattern not in current:
                return "That pattern is not active."
            settings = await self.db.get_settings(chat_id)
            added = list(settings.get(f"{PATTERN_ADD_PREFIX}{detector}", []))
            if pattern in added:
                added.remove(pattern)
                await self.db.set_setting(
                    chat_id, f"{PATTERN_ADD_PREFIX}{detector}", added
                )
            file_rules = await self.rules.file_rules(chat_id)
            if pattern in getattr(file_rules.detectors, detector).patterns:
                removed = list(
                    settings.get(f"{PATTERN_DEL_PREFIX}{detector}", [])
                )
                removed.append(pattern)
                await self.db.set_setting(
                    chat_id, f"{PATTERN_DEL_PREFIX}{detector}", removed
                )
            self.rules.invalidate(chat_id)
            return None

    async def log_view(
        self, chat_id: int, page: int, size: int
    ) -> tuple[InputRichMessage, InlineKeyboardMarkup | None]:
        size = log_size(size)
        actions = await self.db.recent_actions(chat_id, size, page * size)
        total = await self.db.count_actions(chat_id)
        title = await self.db.chat_title(chat_id)
        return (
            log_card(title, actions, page, total, size),
            log_keyboard(chat_id, page, total, size),
        )

    async def status_card(self, chat_id: int) -> InputRichMessage:
        ruleset = await self.rules.get(chat_id)
        chat = await self.db.get_chat(chat_id)
        return status_card(
            chat_label(chat_id, chat),
            ruleset.rules,
            await self._rights_line(chat_id),
            chat.raid_until if chat else 0,
            self.engine.now(),
            [
                DetectorRow(d.name, d.weight, d.summary)
                for d in ruleset.detectors
            ],
            ruleset.skipped,
            await self._admin_rows(chat_id),
        )

    async def _rights_line(self, chat_id: int) -> str:
        try:
            me = await self.bot.me()
            member = await self.bot.get_chat_member(
                chat_id=chat_id, user_id=me.id
            )
        except TelegramAPIError as exc:
            return f"{DOT_GRAY} Unknown ({exc})"
        return describe_rights(member)

    async def _admin_rows(self, chat_id: int) -> list[AdminRow] | None:
        try:
            admins = await self.admins.get_admins(chat_id, fresh=True)
        except AdminLookupError:
            return None
        rows = []
        for user_id, member in admins.items():
            if member.user.is_bot:
                continue
            state = await self.db.subscription_state(chat_id, user_id)
            if state and state[0]:
                label = STATE_SUBSCRIBED
            elif state and state[1] == "blocked":
                label = STATE_BLOCKED
            elif await self.db.has_started(user_id):
                label = STATE_IDLE
            else:
                label = STATE_NOT_STARTED
            rows.append(AdminRow(user_id, member.user.full_name, label))
        return rows


def _penalty_in_force(
    actions: list[ActionRecord], now: int
) -> ActionRecord | None:
    for action in actions:
        if action.action == ACTION_BAN:
            return action
    for action in actions:
        if action.action in (ACTION_RESTRICT, ACTION_RAID_RESTRICT) and (
            action.until_ts is None or action.until_ts > now
        ):
            return action
    return None


def missing_rights(member) -> list[str]:
    if isinstance(member, ChatMemberOwner):
        return []
    if not isinstance(member, ChatMemberAdministrator):
        return ["admin status"]
    missing = []
    if not member.can_delete_messages:
        missing.append("delete messages")
    if not member.can_restrict_members:
        missing.append("ban users")
    return missing


def describe_rights(member) -> str:
    missing = missing_rights(member)
    if not missing:
        return f"{DOT_GREEN} delete, {DOT_GREEN} ban"
    return f"{DOT_RED} missing " + ", ".join(missing)
