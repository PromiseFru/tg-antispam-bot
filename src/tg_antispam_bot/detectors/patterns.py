from __future__ import annotations

import asyncio
import unicodedata

from ..config import KeywordClusterRule, PatternRule
from .base import (
    KIND_CONTEXT,
    DetectionResult,
    Detector,
    MessageContext,
    compile_patterns,
    find_match,
)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


class _RegexDetector(Detector[PatternRule]):
    label = "text"

    def __init__(self, rule: PatternRule):
        super().__init__(rule)
        self.patterns = compile_patterns(self.name, rule.patterns)

    def subject(self, ctx: MessageContext) -> str:
        return ctx.scan_text

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        subject = _normalize(self.subject(ctx))
        if not subject or not self.patterns:
            return None
        pattern = await asyncio.to_thread(find_match, self.patterns, subject)
        if pattern is None:
            return None
        return self.hit(f"{self.label} matches /{pattern.pattern}/")


class TextPatternDetector(_RegexDetector):
    name = "text_pattern"
    checks_content = True
    description = "Text matches a blocked pattern"


class LinkPatternDetector(_RegexDetector):
    name = "link_pattern"
    checks_content = True
    description = "Link matches a blocked pattern"
    label = "link"

    def subject(self, ctx: MessageContext) -> str:
        return "\n".join([ctx.scan_text, *ctx.urls])


class DisplayNamePatternDetector(_RegexDetector):
    name = "display_name_pattern"
    kind = KIND_CONTEXT
    description = "Sender name matches a blocked pattern"
    label = "display name"

    def subject(self, ctx: MessageContext) -> str:
        return ctx.display_name


class KeywordClusterDetector(_RegexDetector):
    name = "keyword_cluster"
    checks_content = True
    description = "Text matches several blocked patterns"

    def __init__(self, rule: KeywordClusterRule):
        super().__init__(rule)
        self.min_matches = rule.min_matches

    def _matching(self, subject: str) -> list[str]:
        return [
            pattern.pattern
            for pattern in self.patterns
            if find_match([pattern], subject)
        ]

    async def check(self, ctx: MessageContext) -> DetectionResult | None:
        subject = _normalize(self.subject(ctx))
        if not subject or not self.patterns:
            return None
        matched = await asyncio.to_thread(self._matching, subject)
        if len(matched) < self.min_matches:
            return None
        sample = ", ".join(f"/{pattern}/" for pattern in matched[:3])
        return self.hit(
            f"text matches {len(matched)} blocked patterns ({sample})"
        )
