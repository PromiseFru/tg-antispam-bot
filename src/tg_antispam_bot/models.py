from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

RESOLUTION_CONFIRMED = "confirm"


class Base(DeclarativeBase):
    type_annotation_map = {
        int: BigInteger,
        str: String,
        list[str]: JSON,
        list[int]: JSON,
    }


class ChatRecord(Base):
    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(default="")
    type: Mapped[str] = mapped_column(default="")
    active: Mapped[bool] = mapped_column(default=True)
    raid_until: Mapped[int] = mapped_column(default=0)
    raid_suppressed_until: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[int] = mapped_column(default=0)


class ChatMigration(Base):
    __tablename__ = "chat_migrations"

    new_chat_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )
    old_chat_id: Mapped[int]


class ChatSetting(Base):
    __tablename__ = "settings"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Join(Base):
    __tablename__ = "joins"
    __table_args__ = (
        Index("idx_joins_chat_time", "chat_id", "joined_at"),
        Index("idx_joins_chat_user", "chat_id", "user_id", "joined_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    chat_id: Mapped[int]
    user_id: Mapped[int]
    joined_at: Mapped[int]


class MemberActivity(Base):
    __tablename__ = "member_activity"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_count: Mapped[int] = mapped_column(default=0)
    last_seen: Mapped[int] = mapped_column(default=0)


class TrustedUser(Base):
    __tablename__ = "trusted_users"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)


class RecentMessage(Base):
    __tablename__ = "recent_messages"
    __table_args__ = (Index("idx_recent_messages_time", "created_at"),)

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )
    created_at: Mapped[int]


class ActionRecord(Base):
    __tablename__ = "actions_log"
    __table_args__ = (
        Index("idx_actions_chat_time", "chat_id", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    chat_id: Mapped[int]
    user_id: Mapped[int]
    user_name: Mapped[str] = mapped_column(default="")
    score: Mapped[int] = mapped_column(default=0)
    reasons: Mapped[list[str]] = mapped_column(default=list)
    message_text: Mapped[str] = mapped_column(default="")
    action: Mapped[str]
    until_ts: Mapped[int | None]
    deleted_count: Mapped[int] = mapped_column(default=0)
    errors: Mapped[list[str]] = mapped_column(default=list)
    created_at: Mapped[int]
    resolved_at: Mapped[int | None]
    resolved_by: Mapped[int | None]
    resolved_by_name: Mapped[str | None]
    resolution: Mapped[str | None]

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None

    @property
    def is_overturned(self) -> bool:
        return self.is_resolved and self.resolution != RESOLUTION_CONFIRMED


class ActionDm(Base):
    __tablename__ = "action_dms"
    __table_args__ = (Index("idx_action_dms_action", "action_id"),)

    recipient_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )
    message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )
    action_id: Mapped[int]


class AdminSubscription(Base):
    __tablename__ = "admin_subscriptions"
    __table_args__ = (Index("idx_subscriptions_user", "user_id"),)

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    active: Mapped[bool] = mapped_column(default=True)
    inactive_reason: Mapped[str | None]
    updated_at: Mapped[int] = mapped_column(default=0)


class AdminPref(Base):
    __tablename__ = "admin_prefs"

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(default="all")
    started_at: Mapped[int | None]


class DigestRecord(Base):
    __tablename__ = "digests"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    recipient_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=False
    )
    message_id: Mapped[int | None]
    started_at: Mapped[int]
    updated_at: Mapped[int]
    expires_at: Mapped[int]
    total: Mapped[int] = mapped_column(default=0)
    deleted: Mapped[int] = mapped_column(default=0)
    restricted: Mapped[int] = mapped_column(default=0)
    banned: Mapped[int] = mapped_column(default=0)
    latest: Mapped[list[int]] = mapped_column(default=list)
