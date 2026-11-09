from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)

from .admin import AdminCache, AdminLookupError
from .db import (
    ACTION_BAN,
    PREF_BANS_ONLY,
    PREF_DIGEST_ONLY,
    ActionRecord,
    Database,
    chat_label,
)
from .render import (
    action_keyboard,
    digest_card,
    digest_keyboard,
    rich_card,
)
from .rules_store import RulesStore

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 5
MAX_VERIFY_ATTEMPTS = 3
VERIFY_RETRY_SECONDS = 5.0
GROUP_RECIPIENT_INTERVAL = 3.0


class RateLimiter:
    def __init__(
        self,
        rate_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval = 1.0 / rate_per_second
        self.clock = clock
        self._next_slot = 0.0

    async def acquire(self) -> None:
        now = self.clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.interval
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        self._next_slot = max(self._next_slot, self.clock() + seconds)


@dataclass
class SendCard:
    action_id: int
    chat_id: int
    attempt: int = 0


@dataclass
class EditCard:
    action_id: int
    message_id: int


@dataclass
class FlushDigest:
    chat_id: int
    attempt: int = 0


Job = SendCard | EditCard | FlushDigest


@dataclass
class _Recipient:
    limiter: RateLimiter
    jobs: list[Job] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class _GiveUp(Exception):
    pass


class NotifyService:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        rules: RulesStore,
        admins: AdminCache,
        fallback_chat_id: int | None = None,
        per_admin_interval: float | None = None,
        global_per_second: float | None = None,
        digest_edit_interval: float | None = None,
        group_interval: float = GROUP_RECIPIENT_INTERVAL,
        verify_retry_seconds: float = VERIFY_RETRY_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        settings = rules.global_rules
        self.bot = bot
        self.db = db
        self.rules = rules
        self.admins = admins
        self.fallback_chat_id = fallback_chat_id
        self.per_admin_interval = (
            per_admin_interval or settings.notify_per_admin_interval
        )
        self.digest_edit_interval = (
            digest_edit_interval
            if digest_edit_interval is not None
            else settings.digest_edit_interval
        )
        self.group_interval = group_interval
        self.verify_retry_seconds = verify_retry_seconds
        self.preview_chars = settings.message_preview_chars
        self.clock = clock
        self.global_limiter = RateLimiter(
            global_per_second or settings.notify_global_per_second
        )
        self._recipients: dict[int, _Recipient] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_flushes: set[tuple[int, int]] = set()
        self._last_flush: dict[tuple[int, int], float] = {}
        self._digest_lock = asyncio.Lock()
        self._moved: dict[int, int] = {}
        self.on_fallback_moved: Callable[[int], Awaitable[None]] | None = None

    def now(self) -> int:
        return int(self.clock())

    def submit(self, action: ActionRecord) -> None:
        self._spawn(self._publish(action))

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "notify task failed", extra={"error": repr(task.exception())}
            )

    def _recipient(self, recipient_id: int) -> _Recipient:
        state = self._recipients.get(recipient_id)
        if state is None:
            interval = (
                self.per_admin_interval
                if recipient_id > 0
                else max(self.per_admin_interval, self.group_interval)
            )
            state = _Recipient(limiter=RateLimiter(1.0 / interval))
            self._recipients[recipient_id] = state
        return state

    def _enqueue(self, recipient_id: int, job: Job) -> None:
        recipient_id = self._moved.get(recipient_id, recipient_id)
        state = self._recipient(recipient_id)
        state.jobs.append(job)
        if state.task is None or state.task.done():
            state.task = self._spawn(self._drain(recipient_id, state))

    async def _drain(self, recipient_id: int, state: _Recipient) -> None:
        while state.jobs:
            job = state.jobs.pop(0)
            try:
                await self._run(recipient_id, state, job)
            except TelegramMigrateToChat as exc:
                await self._follow_fallback(recipient_id, job, exc)
            except _GiveUp as exc:
                logger.warning(
                    "notification dropped",
                    extra={"recipient": recipient_id, "error": str(exc)},
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "notification failed",
                    extra={"recipient": recipient_id, "error": str(exc)},
                )
            except Exception:
                logger.exception(
                    "notification crashed", extra={"recipient": recipient_id}
                )

    def set_fallback(self, chat_id: int) -> None:
        old_chat_id = self.fallback_chat_id
        self.fallback_chat_id = chat_id
        if old_chat_id is None or old_chat_id == chat_id:
            return
        self._moved[old_chat_id] = chat_id
        self._pending_flushes = {
            key for key in self._pending_flushes if key[0] != old_chat_id
        }
        state = self._recipients.get(old_chat_id)
        jobs = [] if state is None else list(state.jobs)
        if state is not None:
            state.jobs.clear()
        self._spawn(self._take_over(old_chat_id, chat_id, jobs))

    async def _take_over(
        self, old_chat_id: int, new_chat_id: int, jobs: list[Job]
    ) -> None:
        await self.db.move_digests(old_chat_id, new_chat_id)
        for job in jobs:
            if not isinstance(job, EditCard):
                self._enqueue(new_chat_id, job)

    async def _follow_fallback(
        self, recipient_id: int, job: Job, exc: TelegramMigrateToChat
    ) -> None:
        if recipient_id != self.fallback_chat_id:
            logger.warning(
                "recipient chat was upgraded",
                extra={"recipient": recipient_id},
            )
            return
        new_chat_id = exc.migrate_to_chat_id
        self._recipient(recipient_id).jobs.insert(0, job)
        self.set_fallback(new_chat_id)
        if self.on_fallback_moved is not None:
            await self.on_fallback_moved(new_chat_id)

    async def _call(
        self,
        recipient_id: int,
        state: _Recipient,
        make_call: Callable[[], Awaitable[Any]],
    ) -> Any:
        for _ in range(MAX_SEND_ATTEMPTS):
            await state.limiter.acquire()
            await self.global_limiter.acquire()
            try:
                return await make_call()
            except TelegramRetryAfter as exc:
                logger.warning(
                    "notify rate limited",
                    extra={
                        "recipient": recipient_id,
                        "retry_after": exc.retry_after,
                    },
                )
                state.limiter.pause(exc.retry_after)
                if recipient_id > 0:
                    self.global_limiter.pause(exc.retry_after)
        raise _GiveUp("retry attempts exhausted")

    async def _publish(self, action: ActionRecord) -> None:
        chat_id = action.chat_id
        subscribers = await self.db.active_subscribers(chat_id)
        recipients: list[int] = []
        for user_id in subscribers:
            try:
                if await self.admins.is_admin(chat_id, user_id):
                    recipients.append(user_id)
                else:
                    await self._drop(chat_id, user_id)
            except AdminLookupError:
                recipients.append(user_id)

        if not recipients:
            if self.fallback_chat_id is None:
                logger.info(
                    "action logged without recipients",
                    extra={
                        "chat_id": chat_id,
                        "action_id": action.id,
                        "action": action.action,
                    },
                )
                return
            recipients = [self.fallback_chat_id]

        rules = (await self.rules.get(chat_id)).rules.notifications
        now = self.now()
        chat = await self.db.get_chat(chat_id)
        raid = chat is not None and chat.raid_until > now
        recent = await self.db.count_actions_since(
            chat_id, now - rules.digest_window
        )
        burst = raid or recent > rules.digest_threshold

        for recipient in recipients:
            mode = await self.db.get_pref(recipient)
            if mode == PREF_BANS_ONLY and action.action != ACTION_BAN:
                continue
            async with self._digest_lock:
                use_digest = (
                    mode == PREF_DIGEST_ONLY
                    or burst
                    or await self.db.digest_is_open(chat_id, recipient, now)
                )
                if use_digest:
                    await self.db.digest_add(
                        chat_id,
                        recipient,
                        action,
                        now,
                        rules.digest_idle_timeout,
                    )
            if use_digest:
                self._schedule_flush(recipient, chat_id)
            else:
                self._enqueue(recipient, SendCard(action.id, chat_id))

    def _schedule_flush(self, recipient_id: int, chat_id: int) -> None:
        key = (recipient_id, chat_id)
        if key in self._pending_flushes:
            return
        self._pending_flushes.add(key)
        last = self._last_flush.get(key)
        delay = 0.0
        if last is not None:
            delay = max(
                0.0, last + self.digest_edit_interval - time.monotonic()
            )
        if delay == 0.0:
            self._enqueue(recipient_id, FlushDigest(chat_id))
            return

        async def later() -> None:
            await asyncio.sleep(delay)
            self._enqueue(recipient_id, FlushDigest(chat_id))

        self._spawn(later())

    async def _drop(self, chat_id: int, user_id: int) -> None:
        await self.db.drop_subscription(chat_id, user_id)
        logger.info(
            "subscription dropped, no longer admin",
            extra={"chat_id": chat_id, "user_id": user_id},
        )

    async def _allowed(
        self, recipient_id: int, chat_id: int, retry: Callable[[], None]
    ) -> bool:
        if recipient_id == self.fallback_chat_id:
            return True
        if not await self.db.is_subscribed(chat_id, recipient_id):
            return False
        try:
            if await self.admins.is_admin(chat_id, recipient_id):
                return True
        except AdminLookupError as exc:
            logger.warning(
                "admin check unavailable, delaying notification",
                extra={
                    "chat_id": chat_id,
                    "recipient": recipient_id,
                    "error": str(exc),
                },
            )
            retry()
            return False
        await self._drop(chat_id, recipient_id)
        return False

    def _retry_later(
        self, recipient_id: int, job: SendCard | FlushDigest
    ) -> None:
        job.attempt += 1
        if job.attempt >= MAX_VERIFY_ATTEMPTS:
            logger.warning(
                "notification dropped, admin check kept failing",
                extra={"chat_id": job.chat_id, "recipient": recipient_id},
            )
            return

        async def later() -> None:
            await asyncio.sleep(self.verify_retry_seconds)
            if isinstance(job, FlushDigest):
                self._pending_flushes.add((recipient_id, job.chat_id))
            self._enqueue(recipient_id, job)

        self._spawn(later())

    async def _handle_forbidden(self, recipient_id: int) -> None:
        if recipient_id == self.fallback_chat_id:
            logger.error(
                "fallback chat rejected the bot",
                extra={"recipient": recipient_id},
            )
            return
        await self.db.mark_user_blocked(recipient_id, self.now())
        logger.info(
            "recipient blocked the bot", extra={"recipient": recipient_id}
        )

    async def _run(
        self, recipient_id: int, state: _Recipient, job: Job
    ) -> None:
        if isinstance(job, SendCard):
            await self._send_card(recipient_id, state, job)
        elif isinstance(job, EditCard):
            await self._edit_card(recipient_id, state, job)
        else:
            await self._flush_digest(recipient_id, state, job)

    async def _send_card(
        self, recipient_id: int, state: _Recipient, job: SendCard
    ) -> None:
        allowed = await self._allowed(
            recipient_id,
            job.chat_id,
            lambda: self._retry_later(recipient_id, job),
        )
        if not allowed:
            return
        action = await self.db.get_action(job.action_id)
        if action is None:
            return
        title = await self.db.chat_title(job.chat_id)
        try:
            message = await self._call(
                recipient_id,
                state,
                lambda: self.bot.send_rich_message(
                    chat_id=recipient_id,
                    rich_message=rich_card(action, title, self.preview_chars),
                    reply_markup=action_keyboard(action),
                ),
            )
        except TelegramForbiddenError:
            await self._handle_forbidden(recipient_id)
            return
        await self.db.add_action_dm(action.id, recipient_id, message.message_id)

    async def _edit_card(
        self, recipient_id: int, state: _Recipient, job: EditCard
    ) -> None:
        action = await self.db.get_action(job.action_id)
        if action is None:
            return
        if recipient_id != self.fallback_chat_id:
            try:
                if not await self.admins.is_admin(action.chat_id, recipient_id):
                    await self._drop(action.chat_id, recipient_id)
                    return
            except AdminLookupError as exc:
                logger.info(
                    "card edit skipped, admin check unavailable",
                    extra={"recipient": recipient_id, "error": str(exc)},
                )
                return
        title = await self.db.chat_title(action.chat_id)
        try:
            await self._call(
                recipient_id,
                state,
                lambda: self.bot.edit_message_text(
                    chat_id=recipient_id,
                    message_id=job.message_id,
                    rich_message=rich_card(action, title, self.preview_chars),
                    reply_markup=action_keyboard(action),
                ),
            )
        except TelegramForbiddenError:
            await self._handle_forbidden(recipient_id)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                logger.info(
                    "card edit skipped",
                    extra={"recipient": recipient_id, "error": str(exc)},
                )

    async def _flush_digest(
        self, recipient_id: int, state: _Recipient, job: FlushDigest
    ) -> None:
        key = (recipient_id, job.chat_id)
        self._pending_flushes.discard(key)
        allowed = await self._allowed(
            recipient_id,
            job.chat_id,
            lambda: self._retry_later(recipient_id, job),
        )
        if not allowed:
            return
        digest = await self.db.get_digest(job.chat_id, recipient_id)
        if digest is None:
            return
        chat = await self.db.get_chat(job.chat_id)
        title = chat_label(job.chat_id, chat)
        latest = await self.db.get_actions(digest.latest)
        card = digest_card(
            digest, title, latest, chat.raid_until if chat else 0, self.now()
        )
        markup = digest_keyboard(job.chat_id)
        self._last_flush[key] = time.monotonic()
        try:
            if digest.message_id is not None:
                try:
                    await self._call(
                        recipient_id,
                        state,
                        lambda: self.bot.edit_message_text(
                            chat_id=recipient_id,
                            message_id=digest.message_id,
                            rich_message=card,
                            reply_markup=markup,
                        ),
                    )
                    return
                except TelegramBadRequest as exc:
                    if "not modified" in str(exc):
                        return
                    logger.info(
                        "digest edit failed, sending new message",
                        extra={"recipient": recipient_id, "error": str(exc)},
                    )
            message = await self._call(
                recipient_id,
                state,
                lambda: self.bot.send_rich_message(
                    chat_id=recipient_id,
                    rich_message=card,
                    reply_markup=markup,
                ),
            )
            await self.db.digest_set_message(
                job.chat_id, recipient_id, message.message_id
            )
        except TelegramForbiddenError:
            await self._handle_forbidden(recipient_id)

    async def notify_resolved(
        self, action: ActionRecord, skip: tuple[int, int] | None = None
    ) -> None:
        for recipient_id, message_id in await self.db.action_dms(action.id):
            if skip != (recipient_id, message_id):
                self._enqueue(recipient_id, EditCard(action.id, message_id))

    async def join(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self, time_limit: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self.join(), time_limit)
        except TimeoutError:
            logger.warning("notify queue not drained before shutdown")
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
