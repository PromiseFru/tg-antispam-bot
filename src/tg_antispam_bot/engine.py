from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from weakref import WeakValueDictionary

from aiogram.enums import ContentType
from aiogram.types import ChatMemberUpdated, Message, User
from cachetools import TTLCache

from .actions import ActionExecutor, ApplyResult
from .admin import AdminCache, AdminLookupError
from .config import TIER_BAN, TIER_DELETE, TIER_RESTRICT, ChatRules, Tiers
from .db import ACTION_BAN, ACTION_RAID_RESTRICT, ActionRecord, Database
from .detectors.base import (
    KIND_SIGNAL,
    DetectionResult,
    Detector,
    MessageContext,
)
from .detectors.cas import CasClient
from .rules_store import RulesStore

logger = logging.getLogger(__name__)

TIER_RANK = {TIER_DELETE: 1, TIER_RESTRICT: 2, TIER_BAN: 3}
SANCTION_GATE_SECONDS = 300
SANCTION_CACHE_SIZE = 10000

SERVICE_CONTENT = frozenset(
    {
        ContentType.NEW_CHAT_MEMBERS,
        ContentType.LEFT_CHAT_MEMBER,
        ContentType.CHAT_OWNER_LEFT,
        ContentType.CHAT_OWNER_CHANGED,
        ContentType.NEW_CHAT_TITLE,
        ContentType.NEW_CHAT_PHOTO,
        ContentType.DELETE_CHAT_PHOTO,
        ContentType.GROUP_CHAT_CREATED,
        ContentType.SUPERGROUP_CHAT_CREATED,
        ContentType.CHANNEL_CHAT_CREATED,
        ContentType.MESSAGE_AUTO_DELETE_TIMER_CHANGED,
        ContentType.MIGRATE_TO_CHAT_ID,
        ContentType.MIGRATE_FROM_CHAT_ID,
        ContentType.PINNED_MESSAGE,
        ContentType.SUCCESSFUL_PAYMENT,
        ContentType.REFUNDED_PAYMENT,
        ContentType.USERS_SHARED,
        ContentType.USER_SHARED,
        ContentType.CHAT_SHARED,
        ContentType.GIFT,
        ContentType.UNIQUE_GIFT,
        ContentType.GIFT_UPGRADE_SENT,
        ContentType.CONNECTED_WEBSITE,
        ContentType.WRITE_ACCESS_ALLOWED,
        ContentType.PASSPORT_DATA,
        ContentType.PROXIMITY_ALERT_TRIGGERED,
        ContentType.BOOST_ADDED,
        ContentType.CHAT_BACKGROUND_SET,
        ContentType.CHECKLIST_TASKS_DONE,
        ContentType.CHECKLIST_TASKS_ADDED,
        ContentType.DIRECT_MESSAGE_PRICE_CHANGED,
        ContentType.FORUM_TOPIC_CREATED,
        ContentType.FORUM_TOPIC_EDITED,
        ContentType.FORUM_TOPIC_CLOSED,
        ContentType.FORUM_TOPIC_REOPENED,
        ContentType.GENERAL_FORUM_TOPIC_HIDDEN,
        ContentType.GENERAL_FORUM_TOPIC_UNHIDDEN,
        ContentType.GIVEAWAY_CREATED,
        ContentType.GIVEAWAY_WINNERS,
        ContentType.GIVEAWAY_COMPLETED,
        ContentType.MANAGED_BOT_CREATED,
        ContentType.PAID_MESSAGE_PRICE_CHANGED,
        ContentType.POLL_OPTION_ADDED,
        ContentType.POLL_OPTION_DELETED,
        ContentType.SUGGESTED_POST_APPROVED,
        ContentType.SUGGESTED_POST_APPROVAL_FAILED,
        ContentType.SUGGESTED_POST_DECLINED,
        ContentType.SUGGESTED_POST_PAID,
        ContentType.SUGGESTED_POST_REFUNDED,
        ContentType.VIDEO_CHAT_SCHEDULED,
        ContentType.VIDEO_CHAT_STARTED,
        ContentType.VIDEO_CHAT_ENDED,
        ContentType.VIDEO_CHAT_PARTICIPANTS_INVITED,
        ContentType.WEB_APP_DATA,
        ContentType.COMMUNITY_CHAT_ADDED,
        ContentType.COMMUNITY_CHAT_REMOVED,
        ContentType.COMMUNITY_CHAT_JOINED,
    }
)


class ActionSink(Protocol):
    def submit(self, action: ActionRecord) -> None: ...


@dataclass(frozen=True)
class Hit:
    detector: str
    kind: str
    score: int
    reason: str


@dataclass(frozen=True)
class Evaluation:
    score: int
    hits: Sequence[Hit]
    tier: str | None

    @property
    def reasons(self) -> list[str]:
        return [
            f"{hit.detector}: {hit.reason} (+{hit.score} pts)"
            for hit in self.hits
        ]


@dataclass(frozen=True)
class Actor:
    id: int
    name: str


async def evaluate(
    ctx: MessageContext, detectors: Sequence[Detector[Any]], tiers: Tiers
) -> Evaluation:
    outcomes = await asyncio.gather(
        *(detector.check(ctx) for detector in detectors), return_exceptions=True
    )
    fired: list[tuple[Detector[Any], DetectionResult]] = []
    for detector, outcome in zip(detectors, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.error(
                "detector failed",
                extra={"detector": detector.name, "error": repr(outcome)},
            )
        elif isinstance(outcome, DetectionResult):
            fired.append((detector, outcome))
    content_flagged = any(
        detector.checks_content and outcome.score > 0
        for detector, outcome in fired
    )
    hits = [
        Hit(detector.name, detector.kind, outcome.score, outcome.reason)
        for detector, outcome in fired
        if content_flagged or not detector.needs_content
    ]
    score = sum(hit.score for hit in hits)
    has_signal = any(hit.kind == KIND_SIGNAL and hit.score > 0 for hit in hits)
    return Evaluation(
        score=score,
        hits=tuple(hits),
        tier=tiers.resolve(score) if has_signal else None,
    )


def _escalated_tier(tier: str, offence: int, rules: ChatRules) -> str:
    escalation = rules.escalation
    minimum = tier
    if offence >= escalation.ban_after:
        minimum = TIER_BAN
    elif offence >= escalation.restrict_after:
        minimum = TIER_RESTRICT
    return max(tier, minimum, key=TIER_RANK.__getitem__)


def resolve_actor(message: Message) -> Actor | None:
    sender_chat = message.sender_chat
    if sender_chat is not None:
        if sender_chat.id == message.chat.id:
            return None
        return Actor(sender_chat.id, sender_chat.title or str(sender_chat.id))
    user = message.from_user
    if user is None:
        return None
    return Actor(user.id, user.full_name)


def is_scannable(message: Message) -> bool:
    if message.ephemeral_message_id is not None or message.message_id == 0:
        return False
    if message.is_automatic_forward:
        return False
    return message.content_type not in SERVICE_CONTENT


def _sent_at(message: Message, is_edit: bool, now: int) -> int:
    if is_edit and message.edit_date:
        return min(message.edit_date, now)
    return min(int(message.date.timestamp()), now)


class Engine:
    def __init__(
        self,
        db: Database,
        rules: RulesStore,
        admins: AdminCache,
        executor: ActionExecutor,
        sink: ActionSink,
        cas: CasClient | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.rules = rules
        self.admins = admins
        self.executor = executor
        self.sink = sink
        self.cas = cas
        self.clock = clock
        self._locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._sanctioned: TTLCache = TTLCache(
            maxsize=SANCTION_CACHE_SIZE,
            ttl=SANCTION_GATE_SECONDS,
            timer=lambda: self.clock(),
        )
        self._chat_titles: dict[int, str] = {}

    def now(self) -> int:
        return int(self.clock())

    def clear_sanction(self, chat_id: int, user_id: int) -> None:
        self._sanctioned.pop((chat_id, user_id), None)

    def _lock(self, chat_id: int, user_id: int) -> asyncio.Lock:
        key = (chat_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _active_sanction(self, chat_id: int, user_id: int) -> int:
        entry = self._sanctioned.get((chat_id, user_id))
        if entry is None:
            return 0
        rank, until_ts = entry
        if until_ts is not None and until_ts <= self.now():
            self.clear_sanction(chat_id, user_id)
            return 0
        return rank

    async def remember_chat(
        self, chat_id: int, title: str, chat_type: str
    ) -> None:
        if self._chat_titles.get(chat_id) == title:
            return
        await self.db.upsert_chat(chat_id, title, chat_type, self.now())
        self._chat_titles[chat_id] = title

    def forget_chat(self, chat_id: int) -> None:
        self._chat_titles.pop(chat_id, None)

    async def is_exempt(self, chat_id: int, actor_id: int) -> bool:
        if actor_id < 0:
            if await self.admins.linked_chat_id(chat_id) == actor_id:
                return True
        elif await self.admins.is_admin(chat_id, actor_id):
            return True
        return await self.db.is_trusted(chat_id, actor_id)

    async def raid_active(self, chat_id: int) -> bool:
        chat = await self.db.get_chat(chat_id)
        return chat is not None and chat.raid_until > self.now()

    async def handle_message(
        self, message: Message, is_edit: bool = False
    ) -> Evaluation | None:
        chat_id = message.chat.id
        await self.remember_chat(
            chat_id, message.chat.title or str(chat_id), message.chat.type
        )
        if not is_scannable(message):
            return None
        actor = resolve_actor(message)
        if actor is None:
            return None
        ruleset = await self.rules.get(chat_id)
        rules = ruleset.rules
        if not rules.enabled:
            return None
        try:
            if await self.is_exempt(chat_id, actor.id):
                return None
        except AdminLookupError as exc:
            logger.warning(
                "skipping message, admin list unavailable",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            return None

        if self._active_sanction(chat_id, actor.id) >= TIER_RANK[TIER_RESTRICT]:
            await self.executor.delete_messages(chat_id, [message.message_id])
            return None

        now = self.now()
        ctx = await self._build_context(message, actor, rules, is_edit, now)
        evaluation = await evaluate(ctx, ruleset.detectors, rules.tiers)
        tier = evaluation.tier
        if tier is None:
            return evaluation

        logger.info(
            "spam detected",
            extra={
                "chat_id": chat_id,
                "user_id": actor.id,
                "score": evaluation.score,
                "tier": tier,
                "detectors": [hit.detector for hit in evaluation.hits],
            },
        )
        await self._act(
            message, actor, rules, evaluation, tier, ctx.scan_text, now
        )
        return evaluation

    async def _build_context(
        self,
        message: Message,
        actor: Actor,
        rules: ChatRules,
        is_edit: bool,
        now: int,
    ) -> MessageContext:
        chat_id = message.chat.id
        sent_at = _sent_at(message, is_edit, now)
        joined_at = await self.db.last_join(chat_id, actor.id)
        if is_edit:
            count = await self.db.message_count(chat_id, actor.id)
        else:
            count = await self.db.bump_message_count(chat_id, actor.id, now)

        window = rules.detectors.new_member.window
        first_n = rules.detectors.early_messages.first_n
        is_recent_join = joined_at is not None and sent_at - joined_at <= window
        if is_recent_join or count <= first_n:
            await self.db.buffer_message(
                chat_id, actor.id, message.message_id, now
            )

        async def count_joins(seconds: int) -> int:
            return await self.db.count_joins_since(chat_id, sent_at - seconds)

        async def count_user_joins(seconds: int) -> int:
            return await self.db.count_user_joins_since(
                chat_id, actor.id, sent_at - seconds
            )

        async def cas_lookup(user_id: int) -> bool | None:
            if self.cas is None:
                return None
            cas = rules.detectors.cas
            return await self.cas.lookup(user_id, cas.timeout, cas.cache_ttl)

        return MessageContext(
            message=message,
            user_id=actor.id,
            display_name=actor.name,
            now=sent_at,
            is_edit=is_edit,
            joined_at=joined_at,
            message_count=count,
            new_member_window=window,
            raid_active=lambda: self.raid_active(chat_id),
            count_joins=count_joins,
            count_user_joins=count_user_joins,
            cas_lookup=cas_lookup,
        )

    async def _offence_number(
        self, chat_id: int, user_id: int, rules: ChatRules, now: int
    ) -> int:
        if not rules.escalation.enabled:
            return 1
        since = now - rules.escalation.window
        return await self.db.count_offences_since(chat_id, user_id, since) + 1

    async def _act(
        self,
        message: Message,
        actor: Actor,
        rules: ChatRules,
        evaluation: Evaluation,
        tier: str,
        text: str,
        now: int,
    ) -> None:
        chat_id = message.chat.id
        stored_chars = self.rules.global_rules.message_preview_chars
        async with self._lock(chat_id, actor.id):
            reasons = evaluation.reasons
            offence = await self._offence_number(chat_id, actor.id, rules, now)
            escalated = _escalated_tier(tier, offence, rules)
            if escalated != tier:
                tier = escalated
                reasons = [
                    *reasons,
                    f"escalation: offence {offence} "
                    f"in {rules.escalation.window}s",
                ]
                logger.info(
                    "action escalated",
                    extra={
                        "chat_id": chat_id,
                        "user_id": actor.id,
                        "offence": offence,
                        "tier": tier,
                    },
                )
            prior = self._active_sanction(chat_id, actor.id)
            if prior and prior >= TIER_RANK[tier]:
                await self.executor.delete_messages(
                    chat_id, [message.message_id]
                )
                return
            result = await self.executor.apply(
                chat_id=chat_id,
                user_id=actor.id,
                tier=tier,
                message_id=message.message_id,
                restrict_seconds=rules.restrict_duration,
                revoke_messages=rules.ban_revoke_messages,
                now=now,
            )
            if tier in (TIER_RESTRICT, TIER_BAN) and not result.errors:
                self._sanctioned[(chat_id, actor.id)] = (
                    TIER_RANK[tier],
                    result.until_ts,
                )
            record = await self.db.insert_action(
                chat_id=chat_id,
                user_id=actor.id,
                user_name=actor.name,
                score=evaluation.score,
                reasons=reasons,
                message_text=text[:stored_chars],
                action=tier,
                until_ts=result.until_ts,
                deleted_count=result.deleted_count,
                errors=result.errors,
                now=now,
            )
        self.sink.submit(record)

    async def handle_join(self, event: ChatMemberUpdated) -> None:
        chat = event.chat
        user = event.new_chat_member.user
        await self.remember_chat(chat.id, chat.title or str(chat.id), chat.type)
        now = self.now()
        joined_at = min(int(event.date.timestamp()), now)
        await self.db.add_join(chat.id, user.id, joined_at)
        ruleset = await self.rules.get(chat.id)
        rules = ruleset.rules
        if not rules.enabled or await self.db.is_trusted(chat.id, user.id):
            return
        if user.is_bot:
            if rules.remove_bots_added_by_members:
                await self._remove_unapproved_bot(event, now)
            return
        if event.new_chat_member.status not in ("member", "restricted"):
            return
        if await self._raid_until(chat.id, rules, now) <= now:
            return
        if now - joined_at > rules.detectors.raid.window:
            return
        until_ts = now + rules.raid_mode.restrict_duration
        result = await self.executor.restrict_joiner(chat.id, user.id, until_ts)
        await self._record(
            chat.id, user, "raid mode: new joiner restricted", result, now
        )

    async def _remove_unapproved_bot(
        self, event: ChatMemberUpdated, now: int
    ) -> None:
        chat_id = event.chat.id
        adder = event.from_user
        try:
            if await self.admins.is_admin(chat_id, adder.id, fresh=True):
                return
        except AdminLookupError as exc:
            logger.warning(
                "kept added bot, admin list unavailable",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            return
        bot = event.new_chat_member.user
        result = await self.executor.ban_member(chat_id, bot.id)
        reason = f"bot added by a non-admin: {adder.full_name}"
        await self._record(chat_id, bot, reason, result, now, ACTION_BAN)

    async def _record(
        self,
        chat_id: int,
        user: User,
        reason: str,
        result: ApplyResult,
        now: int,
        action: str = ACTION_RAID_RESTRICT,
    ) -> None:
        record = await self.db.insert_action(
            chat_id=chat_id,
            user_id=user.id,
            user_name=user.full_name,
            score=0,
            reasons=[reason],
            message_text="",
            action=action,
            until_ts=result.until_ts,
            deleted_count=0,
            errors=result.errors,
            now=now,
        )
        self.sink.submit(record)

    async def _raid_until(
        self, chat_id: int, rules: ChatRules, now: int
    ) -> int:
        chat = await self.db.get_chat(chat_id)
        if chat is None:
            return 0
        if not rules.raid_mode.auto or chat.raid_suppressed_until > now:
            return chat.raid_until
        detector = rules.detectors.raid
        joins = await self.db.count_joins_since(chat_id, now - detector.window)
        if joins <= detector.joins:
            return chat.raid_until
        if chat.raid_until <= now:
            logger.warning(
                "raid mode triggered",
                extra={"chat_id": chat_id, "joins": joins},
            )
        raid_until = now + rules.raid_mode.duration
        await self.db.set_raid(chat_id, raid_until)
        return raid_until

    async def set_raid_mode(self, chat_id: int, enabled: bool) -> int:
        ruleset = await self.rules.get(chat_id)
        now = self.now()
        if enabled:
            until = now + ruleset.rules.raid_mode.duration
            await self.db.set_raid(chat_id, until)
            return until
        suppressed = now + ruleset.rules.raid_mode.manual_off_cooldown
        await self.db.set_raid(chat_id, 0, suppressed)
        return 0
