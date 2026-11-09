from __future__ import annotations

from aiogram.types import MessageOriginChannel

from ..config import (
    EarlyMessagesRule,
    EditedSpamRule,
    ForwardedFromChannelRule,
    JoinChurnRule,
    NewMemberRule,
    RaidRule,
)
from .base import KIND_CONTEXT, DetectionResult, Detector, MessageContext


class NewMemberDetector(Detector[NewMemberRule]):
    name = "new_member"
    description = "Sender joined recently"
    kind = KIND_CONTEXT

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        if ctx.joined_at is None or ctx.now - ctx.joined_at > self.rule.window:
            return None
        return self.hit(f"joined {ctx.now - ctx.joined_at}s ago")


class EarlyMessagesDetector(Detector[EarlyMessagesRule]):
    name = "early_messages"
    description = "One of the sender's first messages here"
    kind = KIND_CONTEXT

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        first_n = self.rule.first_n
        if ctx.message_count > first_n:
            return None
        return self.hit(
            f"message {ctx.message_count} of the first {first_n} "
            "seen in this chat"
        )


class ForwardedFromChannelDetector(Detector[ForwardedFromChannelRule]):
    name = "forwarded_from_channel"
    description = "New member forwarded a channel post"

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        if not ctx.is_new_member:
            return None
        origin = ctx.message.forward_origin
        if not isinstance(origin, MessageOriginChannel):
            return None
        return self.hit(
            "new member forwarded from channel "
            f"{origin.chat.title or origin.chat.id}"
        )


class EditedSpamDetector(Detector[EditedSpamRule]):
    name = "edited_spam"
    needs_content = True
    description = "Flagged content was added by an edit"
    kind = KIND_CONTEXT

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        if not ctx.is_edit:
            return None
        return self.hit("the flagged content is in an edited message")


class RaidDetector(Detector[RaidRule]):
    name = "raid"
    description = "New member posting during a join surge"
    kind = KIND_CONTEXT

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        if not ctx.is_new_member:
            return None
        if await ctx.raid_active():
            return self.hit(
                "raid mode is active and the sender joined recently"
            )
        joins = self.rule.joins
        window = self.rule.window
        count = await ctx.count_joins(window)
        if count <= joins:
            return None
        return self.hit(f"{count} joins in {window}s (limit {joins})")


class JoinChurnDetector(Detector[JoinChurnRule]):
    name = "join_churn"
    description = "Sender keeps leaving and rejoining"
    kind = KIND_CONTEXT

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        window = self.rule.window
        if ctx.joined_at is None or ctx.now - ctx.joined_at > window:
            return None
        count = await ctx.count_user_joins(window)
        if count < self.rule.joins:
            return None
        return self.hit(f"joined {count} times in {window}s")
