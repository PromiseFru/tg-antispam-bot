from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject

from .access import AccessControl
from .actions import ActionExecutor
from .admin import AdminCache, ModerationService
from .db import ActionRecord, Database
from .detectors.cas import CasClient
from .engine import Engine
from .ephemeral import EphemeralTools
from .notify import NotifyService
from .rules_store import RulesStore

logger = logging.getLogger(__name__)

DRAIN_TIME_LIMIT_SECONDS = 10


class UpdateTracker(BaseMiddleware):
    def __init__(self) -> None:
        self._running: set[asyncio.Task[Any]] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tracker = data["services"].updates
        task = asyncio.current_task()
        if task is None:
            return await handler(event, data)
        tracker._running.add(task)
        try:
            return await handler(event, data)
        finally:
            tracker._running.discard(task)

    async def drain(self, time_limit: float = DRAIN_TIME_LIMIT_SECONDS) -> None:
        if not self._running:
            return
        _, pending = await asyncio.wait(self._running, timeout=time_limit)
        if pending:
            logger.warning(
                "updates still running at shutdown",
                extra={"count": len(pending)},
            )


class CompositeSink:
    def __init__(self, sinks: Sequence):
        self.sinks = sinks

    def submit(self, action: ActionRecord) -> None:
        for sink in self.sinks:
            sink.submit(action)


@dataclass
class Services:
    updates: UpdateTracker
    access: AccessControl
    db: Database
    rules: RulesStore
    admins: AdminCache
    executor: ActionExecutor
    engine: Engine
    notify: NotifyService
    ephemeral: EphemeralTools
    moderation: ModerationService
    cas: CasClient

    async def stop(self) -> None:
        await self.ephemeral.stop()
        await self.notify.stop()
        await self.cas.close()


def build_services(
    bot: Bot,
    db: Database,
    rules: RulesStore,
    allowed_chat_ids: Iterable[int],
    fallback_chat_id: int | None = None,
    **notify_options,
) -> Services:
    access = AccessControl(bot, db, allowed_chat_ids, fallback_chat_id)
    admins = AdminCache(bot, ttl_seconds=rules.global_rules.admin_cache_ttl)
    executor = ActionExecutor(bot, db)
    notify = NotifyService(
        bot, db, rules, admins, fallback_chat_id, **notify_options
    )
    ephemeral = EphemeralTools(bot, db, rules, admins)
    cas = CasClient()
    engine = Engine(
        db,
        rules,
        admins,
        executor,
        sink=CompositeSink([notify, ephemeral]),
        cas=cas,
    )
    moderation = ModerationService(
        bot, db, rules, admins, engine, executor, notify
    )
    notify.on_fallback_moved = access.follow_fallback
    access.on_fallback_moved = notify.set_fallback
    return Services(
        updates=UpdateTracker(),
        access=access,
        db=db,
        rules=rules,
        admins=admins,
        executor=executor,
        engine=engine,
        notify=notify,
        ephemeral=ephemeral,
        moderation=moderation,
        cas=cas,
    )
