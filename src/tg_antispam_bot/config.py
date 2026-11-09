from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pytimeparse2 import parse

HEX_KEY = re.compile(r"[0-9a-fA-F]{64}")
MAX_PATTERN_LENGTH = 200
MAX_CHAT_PATTERNS = 50
LOG_MAX = 10
NO_CHATS = "none"


def split_chat_ids(value: str) -> list[str]:
    if value.strip().lower() == NO_CHATS:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


DESCRIPTION_MAX_LENGTH = 120
RESTRICT_MIN_SECONDS = 60
RESTRICT_MAX_SECONDS = 365 * 86400

TIER_DELETE = "delete"
TIER_RESTRICT = "delete_and_restrict"
TIER_BAN = "delete_and_ban"

DURATION_FORMAT = re.compile(r"(\d+(\.\d+)?[smhdw])+")


def parse_duration(value: Any) -> Any:
    matched = isinstance(value, str) and DURATION_FORMAT.fullmatch(value)
    seconds = parse(value) if matched else None
    if not isinstance(seconds, (int, float)):
        raise ValueError(
            f"{value!r} is not a valid duration, use lowercase s, m, h, d or w "
            "such as 30s, 5m, 1h or 90d"
        )
    return int(seconds) if float(seconds).is_integer() else float(seconds)


WholeSeconds = Annotated[int, BeforeValidator(parse_duration)]
FractionalSeconds = Annotated[float, BeforeValidator(parse_duration)]
PositiveDuration = Annotated[WholeSeconds, Field(gt=0)]
NonNegativeDuration = Annotated[WholeSeconds, Field(ge=0)]
PositiveFractionalDuration = Annotated[FractionalSeconds, Field(gt=0)]
RestrictDuration = Annotated[
    WholeSeconds, Field(ge=RESTRICT_MIN_SECONDS, le=RESTRICT_MAX_SECONDS)
]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ConfigError(Exception):
    pass


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    db_encryption_key: SecretStr = Field(default=...)
    db_path: str = "tg-antispam-bot.db"

    @field_validator("db_encryption_key")
    @classmethod
    def _key_is_hex(cls, value: SecretStr) -> SecretStr:
        if not HEX_KEY.fullmatch(value.get_secret_value()):
            raise ValueError(
                "must be 64 hex characters, "
                "generate one with: openssl rand -hex 32"
            )
        return value


class Settings(DatabaseSettings):
    bot_token: SecretStr = Field(default=...)
    allowed_chat_ids: Annotated[frozenset[int], NoDecode] = Field(default=...)
    rules_path: str = "rules.yaml"
    fallback_log_chat_id: int | None = None
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _log_level_is_known(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError("must be a level such as DEBUG, INFO or WARNING")
        return level

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _split_chat_ids(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return frozenset(split_chat_ids(value))

    @field_validator("allowed_chat_ids")
    @classmethod
    def _chat_ids_are_groups(cls, value: frozenset[int]) -> frozenset[int]:
        if any(chat_id >= 0 for chat_id in value):
            raise ValueError(
                "must contain only group IDs, which are negative numbers"
            )
        return value

    @field_validator("bot_token")
    @classmethod
    def _token_is_well_formed(cls, value: SecretStr) -> SecretStr:
        from aiogram.utils.token import TokenValidationError, validate_token

        try:
            validate_token(value.get_secret_value())
        except TokenValidationError as exc:
            raise ValueError("is not a valid bot token") from exc
        return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GlobalRules(_Model):
    admin_cache_ttl: PositiveDuration = 60
    notify_per_admin_interval: FractionalSeconds = Field(default=1.0, ge=1.0)
    notify_global_per_second: float = Field(default=25.0, gt=0, lt=30)
    digest_edit_interval: PositiveFractionalDuration = 3.0
    cleanup_interval: PositiveDuration = 3600
    joins_retention: PositiveDuration = 7 * 86400
    recent_messages_retention: PositiveDuration = 2 * 86400
    activity_retention: PositiveDuration = 90 * 86400
    actions_retention: PositiveDuration = 90 * 86400
    message_text_retention: PositiveDuration = 14 * 86400
    message_preview_chars: int = Field(default=300, ge=1, le=1000)


class Tiers(_Model):
    delete: PositiveInt = 50
    delete_and_restrict: PositiveInt = 70
    delete_and_ban: PositiveInt = 100

    @model_validator(mode="after")
    def _ascending(self) -> Tiers:
        if not self.delete <= self.delete_and_restrict <= self.delete_and_ban:
            raise ValueError(
                "tiers must satisfy "
                "delete <= delete_and_restrict <= delete_and_ban"
            )
        return self

    def resolve(self, score: int) -> str | None:
        if score >= self.delete_and_ban:
            return TIER_BAN
        if score >= self.delete_and_restrict:
            return TIER_RESTRICT
        if score >= self.delete:
            return TIER_DELETE
        return None


class DetectorRule(_Model):
    enabled: bool = True
    weight: NonNegativeInt = 0
    description: str | None = Field(
        default=None, min_length=1, max_length=DESCRIPTION_MAX_LENGTH
    )


class MentionCountRule(DetectorRule):
    weight: NonNegativeInt = 40
    threshold: PositiveInt = 3


class MentionPatternRule(DetectorRule):
    weight: NonNegativeInt = 60
    min_matches: PositiveInt = 2
    patterns: list[str] = Field(default_factory=list)


class NewMemberRule(DetectorRule):
    weight: NonNegativeInt = 20
    window: PositiveDuration = 86400


class EarlyMessagesRule(DetectorRule):
    weight: NonNegativeInt = 10
    first_n: PositiveInt = 3


class PatternRule(DetectorRule):
    patterns: list[str] = Field(default_factory=list)


class TextPatternRule(PatternRule):
    weight: NonNegativeInt = 50


class LinkPatternRule(PatternRule):
    weight: NonNegativeInt = 50


class DisplayNamePatternRule(PatternRule):
    weight: NonNegativeInt = 35


class ForwardedFromChannelRule(DetectorRule):
    weight: NonNegativeInt = 15


class EditedSpamRule(DetectorRule):
    weight: NonNegativeInt = 20


class RaidRule(DetectorRule):
    weight: NonNegativeInt = 10
    joins: PositiveInt = 10
    window: PositiveDuration = 60


class JoinChurnRule(DetectorRule):
    weight: NonNegativeInt = 15
    joins: PositiveInt = 3
    window: PositiveDuration = 600


class CasRule(DetectorRule):
    enabled: bool = False
    weight: NonNegativeInt = 100
    timeout: PositiveFractionalDuration = 2.0
    cache_ttl: PositiveDuration = 3600


class Detectors(_Model):
    mention_count: MentionCountRule = MentionCountRule()
    mention_pattern: MentionPatternRule = MentionPatternRule()
    new_member: NewMemberRule = NewMemberRule()
    early_messages: EarlyMessagesRule = EarlyMessagesRule()
    text_pattern: TextPatternRule = TextPatternRule()
    link_pattern: LinkPatternRule = LinkPatternRule()
    display_name_pattern: DisplayNamePatternRule = DisplayNamePatternRule()
    forwarded_from_channel: ForwardedFromChannelRule = (
        ForwardedFromChannelRule()
    )
    edited_spam: EditedSpamRule = EditedSpamRule()
    raid: RaidRule = RaidRule()
    join_churn: JoinChurnRule = JoinChurnRule()
    cas: CasRule = CasRule()


PATTERN_DETECTORS = tuple(
    name
    for name, rule in Detectors().model_dump().items()
    if "patterns" in rule
)


class RaidRules(_Model):
    auto: bool = True
    duration: PositiveDuration = 1800
    restrict_duration: RestrictDuration = 3600
    manual_off_cooldown: NonNegativeDuration = 900


class EscalationRules(_Model):
    enabled: bool = True
    window: PositiveDuration = 600
    restrict_after: int = Field(default=2, ge=2)
    ban_after: int = Field(default=3, ge=2)

    @model_validator(mode="after")
    def _ascending(self) -> EscalationRules:
        if self.ban_after < self.restrict_after:
            raise ValueError("ban_after must not be below restrict_after")
        return self


class NotificationRules(_Model):
    live_ephemeral_alerts: bool = False
    digest_threshold: NonNegativeInt = 5
    digest_window: PositiveDuration = 60
    digest_idle_timeout: PositiveDuration = 600
    log_limit: int = Field(default=5, ge=1, le=LOG_MAX)


class ChatRules(_Model):
    enabled: bool = True
    tiers: Tiers = Tiers()
    restrict_duration: RestrictDuration = 86400
    ban_revoke_messages: bool = False
    remove_bots_added_by_members: bool = True
    raid_mode: RaidRules = RaidRules()
    escalation: EscalationRules = EscalationRules()
    notifications: NotificationRules = NotificationRules()
    detectors: Detectors = Detectors()


class _RulesDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    global_rules: GlobalRules = Field(
        default_factory=GlobalRules, alias="global"
    )
    defaults: dict[str, Any] = Field(default_factory=dict)
    chats: dict[int, dict[str, Any]] = Field(default_factory=dict)


@dataclass(frozen=True)
class RulesFile:
    global_rules: GlobalRules
    defaults: Mapping[str, Any]
    chats: Mapping[int, Mapping[str, Any]]

    def base_document(self, chat_id: int | None) -> dict[str, Any]:
        return deep_merge(
            copy.deepcopy(dict(self.defaults)),
            self.chats.get(chat_id, {}) if chat_id is not None else {},
        )


def deep_merge(
    base: dict[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def parse_chat_rules(
    document: Mapping[str, Any], path: str = "rules"
) -> ChatRules:
    try:
        return ChatRules.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc


def parse_rules_document(document: Any) -> RulesFile:
    if isinstance(document, Mapping):
        document = {
            key: value for key, value in document.items() if value is not None
        }
    try:
        parsed = _RulesDocument.model_validate(
            document if document is not None else {}
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid rules file: {exc}") from exc
    rules = RulesFile(
        global_rules=parsed.global_rules,
        defaults=parsed.defaults,
        chats=parsed.chats,
    )
    parse_chat_rules(rules.base_document(None), "defaults")
    for chat_id in rules.chats:
        parse_chat_rules(rules.base_document(chat_id), f"chats.{chat_id}")
    return rules


def load_rules_file(path: str | Path) -> RulesFile:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read rules file {path}: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return parse_rules_document(document)
