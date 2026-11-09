from __future__ import annotations

import logging
import time

import aiohttp
from cachetools import TLRUCache

from ..config import CasRule
from .base import DetectionResult, Detector, MessageContext

logger = logging.getLogger(__name__)

CAS_URL = "https://api.cas.chat/check"
CACHE_LIMIT = 10000


class CasClient:
    def __init__(self, url: str = CAS_URL):
        self._url = url
        self._session: aiohttp.ClientSession | None = None
        self._cache: TLRUCache[int, tuple[bool, int]] = TLRUCache(
            maxsize=CACHE_LIMIT,
            ttu=lambda key, value, now: now + value[1],
            timer=time.monotonic,
        )

    async def lookup(
        self, user_id: int, time_limit: float, cache_seconds: int
    ) -> bool | None:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached[0]
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                self._url,
                params={"user_id": str(user_id)},
                timeout=aiohttp.ClientTimeout(total=time_limit),
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            logger.warning(
                "cas lookup failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            return None
        banned = isinstance(payload, dict) and payload.get("ok") is True
        self._cache[user_id] = (banned, cache_seconds)
        return banned

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class CasDetector(Detector[CasRule]):
    name = "cas"
    description = "Listed in the Combot Anti-Spam database"

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        if ctx.user_id < 0:
            return None
        if not (ctx.is_new_member or ctx.message_count <= 1):
            return None
        banned = await ctx.cas_lookup(ctx.user_id)
        if not banned:
            return None
        return self.hit("listed in Combot Anti-Spam")
