from __future__ import annotations

from typing import Any

from ..config import ChatRules
from .base import DetectionResult, Detector, MessageContext
from .cas import CasClient, CasDetector
from .membership import (
    EarlyMessagesDetector,
    EditedSpamDetector,
    ForwardedFromChannelDetector,
    JoinChurnDetector,
    NewMemberDetector,
    RaidDetector,
)
from .mentions import MentionCountDetector, MentionPatternDetector
from .patterns import (
    DisplayNamePatternDetector,
    LinkPatternDetector,
    TextPatternDetector,
)

DETECTOR_CLASSES: tuple[type[Detector[Any]], ...] = (
    MentionCountDetector,
    MentionPatternDetector,
    NewMemberDetector,
    EarlyMessagesDetector,
    TextPatternDetector,
    LinkPatternDetector,
    DisplayNamePatternDetector,
    ForwardedFromChannelDetector,
    EditedSpamDetector,
    RaidDetector,
    JoinChurnDetector,
    CasDetector,
)


def build_detectors(rules: ChatRules) -> list[Detector[Any]]:
    return [
        detector_class(getattr(rules.detectors, detector_class.name))
        for detector_class in DETECTOR_CLASSES
        if getattr(rules.detectors, detector_class.name).enabled
    ]


__all__ = [
    "CasClient",
    "DETECTOR_CLASSES",
    "DetectionResult",
    "Detector",
    "MessageContext",
    "build_detectors",
]
