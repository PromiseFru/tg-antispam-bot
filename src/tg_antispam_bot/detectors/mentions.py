from __future__ import annotations

import asyncio

from ..config import MentionCountRule, MentionPatternRule
from .base import (
    DetectionResult,
    Detector,
    MessageContext,
    compile_patterns,
    find_match,
)


class MentionCountDetector(Detector[MentionCountRule]):
    name = "mention_count"
    checks_content = True
    description = "Many @mentions in one message"

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        threshold = self.rule.threshold
        count = len(ctx.mentions)
        if count < threshold:
            return None
        return self.hit(f"{count} mentions (threshold {threshold})")


class MentionPatternDetector(Detector[MentionPatternRule]):
    name = "mention_pattern"
    checks_content = True
    description = "Mentions usernames matching blocked patterns"

    def __init__(self, rule: MentionPatternRule):
        super().__init__(rule)
        self.patterns = compile_patterns(self.name, rule.patterns)
        self.min_matches = rule.min_matches

    def _matching(self, usernames: list[str]) -> list[str]:
        return [name for name in usernames if find_match(self.patterns, name)]

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        usernames = [
            mention.username for mention in ctx.mentions if mention.username
        ]
        if len(usernames) < self.min_matches:
            return None
        matched = await asyncio.to_thread(self._matching, usernames)
        if len(matched) < self.min_matches:
            return None
        sample = ", ".join(f"@{name}" for name in matched[:3])
        return self.hit(
            f"{len(matched)} mentions match a blocked username pattern "
            f"({sample})"
        )
