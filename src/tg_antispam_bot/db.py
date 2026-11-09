from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import aiosqlite
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Connection,
    CursorResult,
    delete,
    distinct,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_dbapi
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from sqlcipher3 import dbapi2 as sqlcipher

from .config import HEX_KEY, TIER_BAN, TIER_DELETE, TIER_RESTRICT
from .models import (
    RESOLUTION_CONFIRMED,
    ActionDm,
    ActionRecord,
    AdminPref,
    AdminSubscription,
    ChatMigration,
    ChatRecord,
    ChatSetting,
    DigestRecord,
    Join,
    MemberActivity,
    RecentMessage,
    TrustedUser,
)

CHAT_ID_MODELS = (
    ChatSetting,
    Join,
    MemberActivity,
    TrustedUser,
    ActionRecord,
    AdminSubscription,
    DigestRecord,
)

PREF_ALL = "all"
PREF_BANS_ONLY = "bans_only"
PREF_DIGEST_ONLY = "digest_only"
PREF_MODES = (PREF_ALL, PREF_BANS_ONLY, PREF_DIGEST_ONLY)

ACTION_DELETE = TIER_DELETE
ACTION_RESTRICT = TIER_RESTRICT
ACTION_BAN = TIER_BAN
ACTION_RAID_RESTRICT = "raid_restrict"

DIGEST_LATEST_LIMIT = 10
DEFAULT_MIGRATIONS = "tg_antispam_bot:migrations"
DBAPI_ERRORS = (
    "Error",
    "DatabaseError",
    "DataError",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
)


def chat_label(chat_id: int, chat: ChatRecord | None) -> str:
    return chat.title if chat and chat.title else str(chat_id)


class DatabaseKeyError(Exception):
    pass


def open_encrypted(path: str, key: str) -> sqlcipher.Connection:
    if not HEX_KEY.fullmatch(key):
        raise DatabaseKeyError("encryption key must be 64 hex characters")
    connection = sqlcipher.connect(path)
    try:
        connection.execute(f"""PRAGMA key = "x'{key}'" """)
        version = connection.execute("PRAGMA cipher_version").fetchone()
        if not version or not version[0]:
            raise DatabaseKeyError("SQLCipher is not active in this build")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
    except sqlcipher.DatabaseError as exc:
        connection.close()
        raise DatabaseKeyError(
            "could not open the database, "
            "the key is wrong or the file is not encrypted"
        ) from exc
    return connection


def _sqlcipher_dbapi() -> AsyncAdapt_aiosqlite_dbapi:
    dbapi = AsyncAdapt_aiosqlite_dbapi(aiosqlite, sqlcipher)
    for name in DBAPI_ERRORS:
        setattr(dbapi, name, getattr(sqlcipher, name))
    return dbapi


def alembic_config(
    script_location: str = DEFAULT_MIGRATIONS, connection: Any = None
) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", script_location.replace("%", "%%")
    )
    config.attributes["connection"] = connection
    return config


class Database:
    def __init__(self, path: str, key: str):
        if not HEX_KEY.fullmatch(key):
            raise DatabaseKeyError("encryption key must be 64 hex characters")
        self._path = path
        self._key = key
        self._engine: AsyncEngine | None = None
        self._lock = asyncio.Lock()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("database is not connected")
        return self._engine

    async def _create_connection(self) -> aiosqlite.Connection:
        return await aiosqlite.Connection(
            lambda: open_encrypted(self._path, self._key), iter_chunk_size=64
        )

    async def connect(self, script_location: str = DEFAULT_MIGRATIONS) -> None:
        self._engine = create_async_engine(
            "sqlite+aiosqlite://",
            module=_sqlcipher_dbapi(),
            async_creator=self._create_connection,
            poolclass=StaticPool,
            json_serializer=lambda value: json.dumps(value, ensure_ascii=False),
        )
        try:
            await self.upgrade(script_location)
        except BaseException:
            await self.close()
            raise

    async def upgrade(
        self, script_location: str = DEFAULT_MIGRATIONS, revision: str = "head"
    ) -> None:
        def run(connection: Connection) -> None:
            command.upgrade(
                alembic_config(script_location, connection), revision
            )

        async with self._lock:
            async with self.engine.begin() as connection:
                await connection.run_sync(run)

    async def downgrade(
        self, revision: str, script_location: str = DEFAULT_MIGRATIONS
    ) -> None:
        def run(connection: Connection) -> None:
            command.downgrade(
                alembic_config(script_location, connection), revision
            )

        async with self._lock:
            async with self.engine.begin() as connection:
                await connection.run_sync(run)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._lock:
            async with AsyncSession(
                self.engine, expire_on_commit=False
            ) as session:
                async with session.begin():
                    yield session

    async def _scalar(self, statement: Any) -> Any:
        async with self.session() as session:
            return await session.scalar(statement)

    async def _scalars(self, statement: Any) -> list[Any]:
        async with self.session() as session:
            return list((await session.scalars(statement)).all())

    async def _execute(self, statement: Any) -> int:
        async with self.session() as session:
            result = await session.execute(statement)
            return cast("CursorResult[Any]", result).rowcount

    async def journal_mode(self) -> str:
        async with self._lock:
            async with self.engine.connect() as connection:
                return (
                    await connection.exec_driver_sql("PRAGMA journal_mode")
                ).scalar_one()

    async def upsert_chat(
        self,
        chat_id: int,
        title: str,
        chat_type: str,
        now: int,
        active: bool = True,
    ) -> None:
        statement = insert(ChatRecord).values(
            chat_id=chat_id,
            title=title,
            type=chat_type,
            active=active,
            updated_at=now,
        )
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[ChatRecord.chat_id],
                set_={
                    "title": statement.excluded.title,
                    "type": statement.excluded.type,
                    "active": statement.excluded.active,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    async def set_chat_active(
        self, chat_id: int, active: bool, now: int
    ) -> None:
        await self._execute(
            update(ChatRecord)
            .where(ChatRecord.chat_id == chat_id)
            .values(active=active, updated_at=now)
        )

    async def get_chat(self, chat_id: int) -> ChatRecord | None:
        async with self.session() as session:
            return await session.get(ChatRecord, chat_id)

    async def chat_title(self, chat_id: int) -> str:
        return chat_label(chat_id, await self.get_chat(chat_id))

    async def active_chats(self) -> list[ChatRecord]:
        return await self._scalars(
            select(ChatRecord)
            .where(ChatRecord.active.is_(True))
            .order_by(ChatRecord.title)
        )

    async def set_raid(
        self, chat_id: int, raid_until: int, suppressed_until: int = 0
    ) -> None:
        await self._execute(
            update(ChatRecord)
            .where(ChatRecord.chat_id == chat_id)
            .values(
                raid_until=raid_until, raid_suppressed_until=suppressed_until
            )
        )

    async def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        async with self.session() as session:
            for model in CHAT_ID_MODELS:
                await session.execute(
                    update(model)
                    .prefix_with("OR REPLACE")
                    .where(model.chat_id == old_chat_id)
                    .values(chat_id=new_chat_id)
                )
            await session.execute(
                delete(RecentMessage).where(
                    RecentMessage.chat_id == old_chat_id
                )
            )
            await session.execute(
                update(ChatRecord)
                .prefix_with("OR REPLACE")
                .where(ChatRecord.chat_id == old_chat_id)
                .values(chat_id=new_chat_id, type="supergroup")
            )

    async def add_chat_migration(
        self, old_chat_id: int, new_chat_id: int
    ) -> None:
        statement = insert(ChatMigration).values(
            new_chat_id=new_chat_id, old_chat_id=old_chat_id
        )
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[ChatMigration.new_chat_id],
                set_={"old_chat_id": statement.excluded.old_chat_id},
            )
        )

    async def chat_migrations(self) -> list[tuple[int, int]]:
        async with self.session() as session:
            rows = await session.execute(
                select(ChatMigration.old_chat_id, ChatMigration.new_chat_id)
            )
            return [(row.old_chat_id, row.new_chat_id) for row in rows]

    async def migrated_from(self, chat_id: int) -> int | None:
        return await self._scalar(
            select(ChatMigration.old_chat_id).where(
                ChatMigration.new_chat_id == chat_id
            )
        )

    async def get_settings(self, chat_id: int) -> dict[str, Any]:
        rows = await self._scalars(
            select(ChatSetting).where(ChatSetting.chat_id == chat_id)
        )
        return {row.key: row.value for row in rows}

    async def set_setting(self, chat_id: int, key: str, value: Any) -> None:
        statement = insert(ChatSetting).values(
            chat_id=chat_id, key=key, value=value
        )
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[ChatSetting.chat_id, ChatSetting.key],
                set_={"value": statement.excluded.value},
            )
        )

    async def add_join(self, chat_id: int, user_id: int, now: int) -> None:
        async with self.session() as session:
            session.add(Join(chat_id=chat_id, user_id=user_id, joined_at=now))

    async def last_join(self, chat_id: int, user_id: int) -> int | None:
        return await self._scalar(
            select(func.max(Join.joined_at)).where(
                Join.chat_id == chat_id, Join.user_id == user_id
            )
        )

    async def count_joins_since(self, chat_id: int, since: int) -> int:
        return await self._scalar(
            select(func.count(distinct(Join.user_id))).where(
                Join.chat_id == chat_id, Join.joined_at >= since
            )
        )

    async def count_user_joins_since(
        self, chat_id: int, user_id: int, since: int
    ) -> int:
        return await self._scalar(
            select(func.count()).where(
                Join.chat_id == chat_id,
                Join.user_id == user_id,
                Join.joined_at >= since,
            )
        )

    async def count_offences_since(
        self, chat_id: int, user_id: int, since: int
    ) -> int:
        return await self._scalar(
            select(func.count()).where(
                ActionRecord.chat_id == chat_id,
                ActionRecord.user_id == user_id,
                ActionRecord.created_at >= since,
                or_(
                    ActionRecord.resolved_at.is_(None),
                    ActionRecord.resolution == RESOLUTION_CONFIRMED,
                ),
                ActionRecord.action != ACTION_RAID_RESTRICT,
            )
        )

    async def bump_message_count(
        self, chat_id: int, user_id: int, now: int
    ) -> int:
        statement = insert(MemberActivity).values(
            chat_id=chat_id, user_id=user_id, message_count=1, last_seen=now
        )
        statement = statement.on_conflict_do_update(
            index_elements=[MemberActivity.chat_id, MemberActivity.user_id],
            set_={
                "message_count": MemberActivity.message_count + 1,
                "last_seen": statement.excluded.last_seen,
            },
        ).returning(MemberActivity.message_count)
        return await self._scalar(statement)

    async def message_count(self, chat_id: int, user_id: int) -> int:
        count = await self._scalar(
            select(MemberActivity.message_count).where(
                MemberActivity.chat_id == chat_id,
                MemberActivity.user_id == user_id,
            )
        )
        return count or 0

    async def clear_member_history(self, chat_id: int, user_id: int) -> None:
        async with self.session() as session:
            for model in (Join, RecentMessage):
                await session.execute(
                    delete(model).where(
                        model.chat_id == chat_id, model.user_id == user_id
                    )
                )

    async def add_trusted(self, chat_id: int, user_id: int) -> None:
        await self._execute(
            insert(TrustedUser)
            .values(chat_id=chat_id, user_id=user_id)
            .on_conflict_do_nothing()
        )

    async def remove_trusted(self, chat_id: int, user_id: int) -> bool:
        removed = await self._execute(
            delete(TrustedUser).where(
                TrustedUser.chat_id == chat_id, TrustedUser.user_id == user_id
            )
        )
        return removed > 0

    async def is_trusted(self, chat_id: int, user_id: int) -> bool:
        found = await self._scalar(
            select(TrustedUser.user_id).where(
                TrustedUser.chat_id == chat_id, TrustedUser.user_id == user_id
            )
        )
        return found is not None

    async def buffer_message(
        self, chat_id: int, user_id: int, message_id: int, now: int
    ) -> None:
        await self._execute(
            insert(RecentMessage)
            .values(
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )

    async def buffered_messages(self, chat_id: int, user_id: int) -> list[int]:
        return await self._scalars(
            select(RecentMessage.message_id).where(
                RecentMessage.chat_id == chat_id,
                RecentMessage.user_id == user_id,
            )
        )

    async def unbuffer_message(
        self, chat_id: int, user_id: int, message_id: int
    ) -> None:
        await self._execute(
            delete(RecentMessage).where(
                RecentMessage.chat_id == chat_id,
                RecentMessage.user_id == user_id,
                RecentMessage.message_id == message_id,
            )
        )

    async def clear_buffer(self, chat_id: int, user_id: int) -> None:
        await self._execute(
            delete(RecentMessage).where(
                RecentMessage.chat_id == chat_id,
                RecentMessage.user_id == user_id,
            )
        )

    async def insert_action(
        self,
        chat_id: int,
        user_id: int,
        user_name: str,
        score: int,
        reasons: Iterable[str],
        message_text: str,
        action: str,
        until_ts: int | None,
        deleted_count: int,
        errors: Iterable[str],
        now: int,
    ) -> ActionRecord:
        record = ActionRecord(
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            score=score,
            reasons=list(reasons),
            message_text=message_text,
            action=action,
            until_ts=until_ts,
            deleted_count=deleted_count,
            errors=list(errors),
            created_at=now,
            resolved_at=None,
            resolved_by=None,
            resolved_by_name=None,
            resolution=None,
        )
        async with self.session() as session:
            session.add(record)
        return record

    async def open_actions(
        self, chat_id: int, user_id: int
    ) -> list[ActionRecord]:
        return await self._scalars(
            select(ActionRecord).where(
                ActionRecord.chat_id == chat_id,
                ActionRecord.user_id == user_id,
                ActionRecord.resolved_at.is_(None),
            )
        )

    async def get_action(self, action_id: int) -> ActionRecord | None:
        async with self.session() as session:
            return await session.get(ActionRecord, action_id)

    async def get_actions(
        self, action_ids: Sequence[int]
    ) -> list[ActionRecord]:
        if not action_ids:
            return []
        return await self._scalars(
            select(ActionRecord)
            .where(ActionRecord.id.in_(action_ids))
            .order_by(ActionRecord.id.desc())
        )

    async def recent_actions(
        self, chat_id: int, limit: int, offset: int = 0
    ) -> list[ActionRecord]:
        return await self._scalars(
            select(ActionRecord)
            .where(ActionRecord.chat_id == chat_id)
            .order_by(ActionRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )

    async def count_actions(self, chat_id: int) -> int:
        return await self._scalar(
            select(func.count()).where(ActionRecord.chat_id == chat_id)
        )

    async def count_actions_since(self, chat_id: int, since: int) -> int:
        return await self._scalar(
            select(func.count()).where(
                ActionRecord.chat_id == chat_id,
                ActionRecord.created_at >= since,
            )
        )

    async def claim_resolution(
        self,
        action_id: int,
        resolver_id: int,
        resolver_name: str,
        resolution: str,
        now: int,
    ) -> bool:
        claimed = await self._execute(
            update(ActionRecord)
            .where(
                ActionRecord.id == action_id, ActionRecord.resolved_at.is_(None)
            )
            .values(
                resolved_at=now,
                resolved_by=resolver_id,
                resolved_by_name=resolver_name,
                resolution=resolution,
            )
        )
        return claimed > 0

    async def release_resolution(self, action_id: int) -> None:
        await self._execute(
            update(ActionRecord)
            .where(ActionRecord.id == action_id)
            .values(
                resolved_at=None,
                resolved_by=None,
                resolved_by_name=None,
                resolution=None,
            )
        )

    async def add_action_dm(
        self, action_id: int, recipient_id: int, message_id: int
    ) -> None:
        await self._execute(
            insert(ActionDm)
            .values(
                action_id=action_id,
                recipient_id=recipient_id,
                message_id=message_id,
            )
            .on_conflict_do_nothing()
        )

    async def action_dms(self, action_id: int) -> list[tuple[int, int]]:
        async with self.session() as session:
            rows = await session.execute(
                select(ActionDm.recipient_id, ActionDm.message_id).where(
                    ActionDm.action_id == action_id
                )
            )
            return [(row.recipient_id, row.message_id) for row in rows]

    async def set_subscription(
        self, chat_id: int, user_id: int, active: bool, now: int
    ) -> None:
        statement = insert(AdminSubscription).values(
            chat_id=chat_id,
            user_id=user_id,
            active=active,
            inactive_reason=None,
            updated_at=now,
        )
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[
                    AdminSubscription.chat_id,
                    AdminSubscription.user_id,
                ],
                set_={
                    "active": statement.excluded.active,
                    "inactive_reason": None,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    async def drop_subscription(self, chat_id: int, user_id: int) -> None:
        async with self.session() as session:
            await session.execute(
                delete(AdminSubscription).where(
                    AdminSubscription.chat_id == chat_id,
                    AdminSubscription.user_id == user_id,
                )
            )
            await session.execute(
                delete(DigestRecord).where(
                    DigestRecord.chat_id == chat_id,
                    DigestRecord.recipient_id == user_id,
                )
            )

    async def mark_user_blocked(self, user_id: int, now: int) -> None:
        await self._execute(
            update(AdminSubscription)
            .where(
                AdminSubscription.user_id == user_id,
                AdminSubscription.active.is_(True),
            )
            .values(active=False, inactive_reason="blocked", updated_at=now)
        )

    async def reactivate_blocked(self, user_id: int, now: int) -> None:
        await self._execute(
            update(AdminSubscription)
            .where(
                AdminSubscription.user_id == user_id,
                AdminSubscription.inactive_reason == "blocked",
            )
            .values(active=True, inactive_reason=None, updated_at=now)
        )

    async def active_subscribers(self, chat_id: int) -> list[int]:
        return await self._scalars(
            select(AdminSubscription.user_id).where(
                AdminSubscription.chat_id == chat_id,
                AdminSubscription.active.is_(True),
            )
        )

    async def is_subscribed(self, chat_id: int, user_id: int) -> bool:
        found = await self._scalar(
            select(AdminSubscription.user_id).where(
                AdminSubscription.chat_id == chat_id,
                AdminSubscription.user_id == user_id,
                AdminSubscription.active.is_(True),
            )
        )
        return found is not None

    async def subscription_state(
        self, chat_id: int, user_id: int
    ) -> tuple[bool, str | None] | None:
        record = await self._scalar(
            select(AdminSubscription).where(
                AdminSubscription.chat_id == chat_id,
                AdminSubscription.user_id == user_id,
            )
        )
        return (record.active, record.inactive_reason) if record else None

    async def mark_started(self, user_id: int, now: int) -> None:
        statement = insert(AdminPref).values(user_id=user_id, started_at=now)
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[AdminPref.user_id],
                set_={"started_at": statement.excluded.started_at},
            )
        )

    async def has_started(self, user_id: int) -> bool:
        started = await self._scalar(
            select(AdminPref.started_at).where(AdminPref.user_id == user_id)
        )
        return started is not None

    async def get_pref(self, user_id: int) -> str:
        mode = await self._scalar(
            select(AdminPref.mode).where(AdminPref.user_id == user_id)
        )
        return mode or PREF_ALL

    async def set_pref(self, user_id: int, mode: str) -> None:
        statement = insert(AdminPref).values(user_id=user_id, mode=mode)
        await self._execute(
            statement.on_conflict_do_update(
                index_elements=[AdminPref.user_id],
                set_={"mode": statement.excluded.mode},
            )
        )

    async def get_digest(
        self, chat_id: int, recipient_id: int
    ) -> DigestRecord | None:
        async with self.session() as session:
            return await session.get(DigestRecord, (chat_id, recipient_id))

    async def digest_is_open(
        self, chat_id: int, recipient_id: int, now: int
    ) -> bool:
        found = await self._scalar(
            select(DigestRecord.chat_id).where(
                DigestRecord.chat_id == chat_id,
                DigestRecord.recipient_id == recipient_id,
                DigestRecord.expires_at > now,
            )
        )
        return found is not None

    async def digest_add(
        self,
        chat_id: int,
        recipient_id: int,
        action: ActionRecord,
        now: int,
        idle_seconds: int,
    ) -> DigestRecord:
        async with self.session() as session:
            digest = await session.get(DigestRecord, (chat_id, recipient_id))
            if digest is None:
                digest = DigestRecord(
                    chat_id=chat_id, recipient_id=recipient_id
                )
                session.add(digest)
            if digest.expires_at is None or digest.expires_at <= now:
                digest.message_id = None
                digest.started_at = now
                digest.total = 0
                digest.deleted = 0
                digest.restricted = 0
                digest.banned = 0
                digest.latest = []
            digest.updated_at = now
            digest.expires_at = now + idle_seconds
            digest.total += 1
            digest.deleted += int(action.action == ACTION_DELETE)
            digest.restricted += int(
                action.action in (ACTION_RESTRICT, ACTION_RAID_RESTRICT)
            )
            digest.banned += int(action.action == ACTION_BAN)
            digest.latest = [
                action.id,
                *(item for item in digest.latest if item != action.id),
            ][:DIGEST_LATEST_LIMIT]
            return digest

    async def move_digests(
        self, old_recipient: int, new_recipient: int
    ) -> None:
        await self._execute(
            update(DigestRecord)
            .prefix_with("OR REPLACE")
            .where(DigestRecord.recipient_id == old_recipient)
            .values(recipient_id=new_recipient, message_id=None)
        )

    async def digest_set_message(
        self, chat_id: int, recipient_id: int, message_id: int | None
    ) -> None:
        await self._execute(
            update(DigestRecord)
            .where(
                DigestRecord.chat_id == chat_id,
                DigestRecord.recipient_id == recipient_id,
            )
            .values(message_id=message_id)
        )

    async def cleanup(
        self,
        now: int,
        joins_retention: int,
        recent_messages_retention: int,
        activity_retention: int,
        actions_retention: int,
        message_text_retention: int,
    ) -> None:
        cutoff = now - actions_retention
        async with self.session() as session:
            await session.execute(
                update(ActionRecord)
                .where(
                    ActionRecord.created_at < now - message_text_retention,
                    ActionRecord.message_text != "",
                )
                .values(message_text="")
            )
            expired = select(ActionRecord.id).where(
                ActionRecord.created_at < cutoff
            )
            await session.execute(
                delete(ActionDm).where(ActionDm.action_id.in_(expired))
            )
            await session.execute(
                delete(ActionRecord).where(ActionRecord.created_at < cutoff)
            )
            await session.execute(
                delete(Join).where(Join.joined_at < now - joins_retention)
            )
            await session.execute(
                delete(RecentMessage).where(
                    RecentMessage.created_at < now - recent_messages_retention
                )
            )
            await session.execute(
                delete(DigestRecord).where(DigestRecord.expires_at <= now)
            )
            await session.execute(
                delete(MemberActivity).where(
                    MemberActivity.last_seen < now - activity_retention
                )
            )
