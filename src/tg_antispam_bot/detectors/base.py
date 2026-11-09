from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar, Generic, TypeVar

import regex
from aiogram.types import Message, MessageEntity

from ..config import DetectorRule
from ..pattern_probe import COMPILE_FLAGS

logger = logging.getLogger(__name__)

KIND_SIGNAL = "signal"
KIND_CONTEXT = "context"
REGEX_TIMEOUT_SECONDS = 0.05
USERNAME_IN_TEXT = regex.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{4,31})")
RuleT = TypeVar("RuleT", bound=DetectorRule)


@dataclass(frozen=True)
class DetectionResult:
    score: int
    reason: str


@dataclass(frozen=True)
class Mention:
    username: str | None
    user_id: int | None


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


async def _no_joins(seconds: int) -> int:
    return 0


async def _no_raid() -> bool:
    return False


async def _no_cas(user_id: int) -> bool | None:
    return None


@dataclass
class MessageContext:
    message: Message
    user_id: int
    display_name: str
    now: int
    is_edit: bool = False
    joined_at: int | None = None
    message_count: int = 1
    new_member_window: int = 86400
    raid_active: Callable[[], Awaitable[bool]] = field(default=_no_raid)
    count_joins: Callable[[int], Awaitable[int]] = field(default=_no_joins)
    count_user_joins: Callable[[int], Awaitable[int]] = field(default=_no_joins)
    cas_lookup: Callable[[int], Awaitable[bool | None]] = field(default=_no_cas)

    @cached_property
    def text(self) -> str:
        return self.message.text or self.message.caption or ""

    @cached_property
    def scan_text(self) -> str:
        return "\n".join(part for part in (self.text, self.extra_text) if part)

    @cached_property
    def extra_text(self) -> str:
        message = self.message
        parts: list[str | None] = []
        if message.poll is not None:
            parts.append(message.poll.question)
            parts.extend(option.text for option in message.poll.options)
            parts.append(message.poll.explanation)
        if message.contact is not None:
            parts.append(message.contact.first_name)
            parts.append(message.contact.last_name)
        if message.venue is not None:
            parts.append(message.venue.title)
            parts.append(message.venue.address)
        if message.game is not None:
            parts.append(message.game.title)
            parts.append(message.game.description)
            parts.append(message.game.text)
        if message.invoice is not None:
            parts.append(message.invoice.title)
            parts.append(message.invoice.description)
        if message.checklist is not None:
            parts.append(message.checklist.title)
            parts.extend(task.text for task in message.checklist.tasks)
        if message.giveaway is not None:
            parts.append(message.giveaway.prize_description)
        if message.rich_message is not None:
            parts.extend(_strings(message.rich_message.model_dump()))
        return "\n".join(part for part in parts if part)

    @cached_property
    def entities(self) -> list[MessageEntity]:
        return list(
            self.message.entities or self.message.caption_entities or []
        )

    @cached_property
    def mentions(self) -> list[Mention]:
        found: list[Mention] = []
        for entity in self.entities:
            if entity.type == "mention":
                found.append(
                    Mention(entity.extract_from(self.text).lstrip("@"), None)
                )
            elif entity.type == "text_mention" and entity.user is not None:
                found.append(Mention(entity.user.username, entity.user.id))
        found.extend(
            Mention(username, None)
            for username in USERNAME_IN_TEXT.findall(self.extra_text)
        )
        distinct = {
            (mention.username or str(mention.user_id)).casefold(): mention
            for mention in reversed(found)
        }
        return list(reversed(distinct.values()))

    @cached_property
    def urls(self) -> list[str]:
        found: list[str] = []
        for entity in self.entities:
            if entity.type == "text_link" and entity.url:
                found.append(entity.url)
            elif entity.type == "url":
                found.append(entity.extract_from(self.text))
        return found

    @property
    def is_new_member(self) -> bool:
        return (
            self.joined_at is not None
            and self.now - self.joined_at <= self.new_member_window
        )


class Detector(ABC, Generic[RuleT]):
    name: ClassVar[str]
    description: ClassVar[str] = ""
    kind: ClassVar[str] = KIND_SIGNAL
    checks_content: ClassVar[bool] = False
    needs_content: ClassVar[bool] = False

    def __init__(self, rule: RuleT):
        self.rule = rule
        self.weight = rule.weight
        self.summary = rule.description or self.description

    def hit(self, reason: str) -> DetectionResult:
        return DetectionResult(self.weight, reason)

    @abstractmethod
    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        raise NotImplementedError


def find_match(
    patterns: Iterable[regex.Pattern[str]], subject: str
) -> regex.Pattern[str] | None:
    for pattern in patterns:
        try:
            if pattern.search(
                subject, concurrent=True, timeout=REGEX_TIMEOUT_SECONDS
            ):
                return pattern
        except TimeoutError:
            logger.warning(
                "pattern timed out", extra={"pattern": pattern.pattern}
            )
    return None


def compile_patterns(
    detector: str, patterns: Iterable[str]
) -> list[regex.Pattern[str]]:
    compiled: list[regex.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(regex.compile(pattern, COMPILE_FLAGS))
        except regex.error as exc:
            logger.error(
                "skipping invalid pattern",
                extra={
                    "detector": detector,
                    "pattern": pattern,
                    "error": str(exc),
                },
            )
    return compiled
