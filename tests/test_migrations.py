from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    UnbanChatMember,
)
from aiogram.types import (
    CallbackQuery,
)
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import exc as sa_exc
from sqlalchemy import inspect, text

import tg_antispam_bot
from conftest import (
    ADMIN_ID,
    CHAT_ID,
    TEST_DB_KEY,
    log_action,
    make_user,
)
from tg_antispam_bot.config import (
    ConfigError,
    parse_rules_document,
)
from tg_antispam_bot.db import (
    Database,
    DatabaseKeyError,
    alembic_config,
)
from tg_antispam_bot.models import ActionRecord, Base, Join
from tg_antispam_bot.render import SURFACE_DM, ActionCb

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = Path(tg_antispam_bot.__file__).parent / "migrations"
NOW = 1_800_000_000


@pytest.fixture
async def path(tmp_path):
    return str(tmp_path / "bot.db")


async def table_names(db: Database) -> set[str]:
    async with db.engine.connect() as connection:
        return set(
            await connection.run_sync(lambda c: inspect(c).get_table_names())
        )


async def revision_of(db: Database) -> str | None:
    async with db.engine.connect() as connection:
        return await connection.run_sync(
            lambda c: MigrationContext.configure(c).get_current_revision()
        )


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


class TestUpgrade:
    async def test_fresh_database_gets_every_table_at_head(self, db):
        assert await table_names(db) == set(Base.metadata.tables) | {
            "alembic_version"
        }
        assert await revision_of(db) == head_revision()

    async def test_models_and_migrations_agree(self, db):
        async with db.engine.connect() as connection:
            differences = await connection.run_sync(
                lambda c: compare_metadata(
                    MigrationContext.configure(c), Base.metadata
                )
            )
        assert differences == []

    async def test_downgrade_then_upgrade_round_trips(self, db):
        await db.downgrade("base")
        assert await table_names(db) == {"alembic_version"}
        await db.upgrade()
        assert await revision_of(db) == head_revision()
        await db.add_trusted(CHAT_ID, 1)
        assert await db.is_trusted(CHAT_ID, 1)

    async def test_new_migration_changes_existing_encrypted_database(
        self, path, tmp_path
    ):
        first = Database(path, TEST_DB_KEY)
        await first.connect()
        await first.add_trusted(CHAT_ID, 1)
        await first.close()

        copied = tmp_path / "migrations"
        shutil.copytree(
            MIGRATIONS, copied, ignore=shutil.ignore_patterns("__pycache__")
        )
        (copied / "versions" / "9001_note.py").write_text(
            f"""
import sqlalchemy as sa
from alembic import op

revision = "9001"
down_revision = {head_revision()!r}
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trusted_users") as batch:
        batch.add_column(
            sa.Column(
                "note", sa.String(), nullable=False, server_default="none"
            )
        )


def downgrade():
    with op.batch_alter_table("trusted_users") as batch:
        batch.drop_column("note")
"""
        )

        second = Database(path, TEST_DB_KEY)
        await second.connect(str(copied))
        try:
            assert await revision_of(second) == "9001"
            async with second.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text("SELECT user_id, note FROM trusted_users")
                    )
                ).one()
            assert tuple(row) == (1, "none")
        finally:
            await second.close()
        assert b"SQLite format 3" not in Path(path).read_bytes()[:16]

    async def test_failed_migration_closes_the_database(self, path):
        before = set(threading.enumerate())
        db = Database(path, TEST_DB_KEY)
        with pytest.raises(CommandError):
            await db.connect("/nonexistent/migrations")
        leftover = [
            t for t in set(threading.enumerate()) - before if not t.daemon
        ]
        assert leftover == []

    async def test_wrong_key_is_still_reported_clearly(self, path):
        first = Database(path, TEST_DB_KEY)
        await first.connect()
        await first.close()
        before = set(threading.enumerate())
        with pytest.raises(DatabaseKeyError):
            await Database(path, "cd" * 32).connect()
        for thread in set(threading.enumerate()) - before:
            thread.join(timeout=2)
            assert not thread.is_alive()


class TestMigrationFiles:
    def scripts(self):
        directory = ScriptDirectory.from_config(alembic_config())
        return list(reversed(list(directory.walk_revisions())))

    def test_history_is_one_straight_line(self):
        scripts = self.scripts()
        assert ScriptDirectory.from_config(alembic_config()).get_heads() == [
            scripts[-1].revision
        ]
        assert scripts[0].down_revision is None
        for earlier, later in zip(scripts, scripts[1:], strict=False):
            assert later.down_revision == earlier.revision

    def test_each_migration_is_in_its_own_file_for_one_table(self):
        created = []
        for script in self.scripts():
            source = Path(script.path).read_text(encoding="utf-8")
            tables = re.findall(r'op\.create_table\(\s*"(\w+)"', source)
            assert len(tables) <= 1, script.path
            created.extend(tables)
        assert sorted(created) == sorted(Base.metadata.tables)

    async def test_each_step_applies_and_reverts_one_table(self, db):
        await db.downgrade("base")
        revisions = [script.revision for script in self.scripts()]
        known = {"alembic_version"}
        history = []
        for revision in revisions:
            await db.upgrade(revision=revision)
            tables = await table_names(db)
            assert len(tables - known) == 1, revision
            history.append(set(tables))
            known = tables
        assert known == set(Base.metadata.tables) | {"alembic_version"}
        for revision, expected in zip(
            reversed(revisions[:-1]), reversed(history[:-1]), strict=True
        ):
            await db.downgrade(revision)
            assert await table_names(db) == expected
        await db.downgrade("base")
        assert await table_names(db) == {"alembic_version"}


class TestCommandLine:
    def run(self, tmp_path, *args, key=TEST_DB_KEY):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "DB_PATH": str(tmp_path / "cli.db"),
            "DB_ENCRYPTION_KEY": key,
        }
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_upgrade_then_check_reports_no_drift(self, tmp_path):
        assert self.run(tmp_path, "upgrade", "head").returncode == 0
        result = self.run(tmp_path, "check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert b"SQLite format 3" not in (tmp_path / "cli.db").read_bytes()[:16]

    def test_database_created_by_cli_opens_in_the_bot(self, tmp_path):
        assert self.run(tmp_path, "upgrade", "head").returncode == 0

        async def open_it():
            db = Database(str(tmp_path / "cli.db"), TEST_DB_KEY)
            await db.connect()
            try:
                return await revision_of(db)
            finally:
                await db.close()

        assert asyncio.run(open_it()) == head_revision()

    def test_wrong_key_fails(self, tmp_path):
        assert self.run(tmp_path, "upgrade", "head").returncode == 0
        result = self.run(tmp_path, "current", key="cd" * 32)
        assert result.returncode != 0
        assert "cd" * 32 not in result.stdout + result.stderr


class TestTransactions:
    async def test_failed_transaction_rolls_back_without_touching_others(
        self, db
    ):
        started = asyncio.Event()

        async def failing():
            async with db.session() as session:
                session.add(Join(chat_id=CHAT_ID, user_id=1, joined_at=NOW))
                await session.flush()
                started.set()
                await asyncio.sleep(0.02)
                raise RuntimeError("boom")

        async def succeeding():
            await started.wait()
            await db.add_join(CHAT_ID, 2, NOW)

        results = await asyncio.gather(
            failing(), succeeding(), return_exceptions=True
        )
        assert isinstance(results[0], RuntimeError)
        assert await db.last_join(CHAT_ID, 1) is None
        assert await db.last_join(CHAT_ID, 2) == NOW

    async def test_concurrent_counters_lose_no_updates(self, db):
        await asyncio.gather(
            *(db.bump_message_count(CHAT_ID, 1, NOW) for _ in range(100))
        )
        assert await db.message_count(CHAT_ID, 1) == 100

    async def test_concurrent_digest_updates_lose_no_updates(self, db):
        action = await log_action(db, user_id=1, action="delete")
        await asyncio.gather(
            *(db.digest_add(CHAT_ID, 5, action, NOW, 600) for _ in range(50))
        )
        assert (await db.get_digest(CHAT_ID, 5)).total == 50

    async def test_duplicate_key_raises_sqlalchemy_error(self, db):
        with pytest.raises(sa_exc.IntegrityError):
            async with db.session() as session:
                for _ in range(2):
                    await session.execute(
                        text(
                            "INSERT INTO trusted_users "
                            "(chat_id, user_id) VALUES (1, 1)"
                        )
                    )


class TestRoundTrips:
    async def test_returned_action_is_usable_after_its_session_closes(self, db):
        action = await db.insert_action(
            CHAT_ID,
            1,
            "Näme",
            7,
            ["пример"],
            "текст",
            "delete",
            None,
            1,
            ["e"],
            NOW,
        )
        assert action.id > 0
        assert action.is_resolved is False
        loaded = await db.get_action(action.id)
        assert loaded.reasons == ["пример"]
        assert loaded.errors == ["e"]
        assert loaded.resolved_by_name is None

    async def test_non_ascii_is_stored_readably(self, db):
        await log_action(db, user_id=1, reasons=["пример"], action="delete")
        async with db.engine.connect() as connection:
            raw = (
                await connection.execute(
                    text("SELECT reasons FROM actions_log")
                )
            ).scalar_one()
        assert "пример" in raw

    @pytest.mark.parametrize("value", [True, False, 5, "x", ["a", "b"]])
    async def test_settings_keep_their_types(self, db, value):
        await db.set_setting(CHAT_ID, "k", value)
        assert (await db.get_settings(CHAT_ID))["k"] == value

    async def test_expired_digest_resets_in_place(self, db):
        action = await log_action(db, user_id=1, action="delete")
        await db.digest_add(CHAT_ID, 5, action, NOW, 10)
        await db.digest_set_message(CHAT_ID, 5, 99)
        digest = await db.digest_add(CHAT_ID, 5, action, NOW + 11, 10)
        assert (digest.total, digest.message_id, digest.started_at) == (
            1,
            None,
            NOW + 11,
        )

    async def test_migrate_chat_moves_rows_and_overwrites_conflicts(self, db):
        new_id = -999
        await db.upsert_chat(CHAT_ID, "Old", "group", NOW)
        await db.upsert_chat(new_id, "Stale", "supergroup", NOW)
        await db.set_setting(CHAT_ID, "k", 1)
        await db.set_setting(new_id, "k", 2)
        await db.add_trusted(CHAT_ID, 1)
        await db.migrate_chat(CHAT_ID, new_id)
        assert await db.get_chat(CHAT_ID) is None
        chat = await db.get_chat(new_id)
        assert (chat.title, chat.type) == ("Old", "supergroup")
        assert (await db.get_settings(new_id))["k"] == 1
        assert await db.is_trusted(new_id, 1)

    async def test_action_id_is_never_reused(self, db):
        first = await log_action(db, user_id=1, action="delete")
        async with db.session() as session:
            await session.delete(await session.get(ActionRecord, first.id))
        second = await log_action(db, user_id=1, action="delete")
        assert second.id > first.id


class TestActionsRetention:
    async def action(self, db, created_at):
        return await log_action(
            db, user_id=1, message_text="text", action="delete", now=created_at
        )

    async def run_cleanup(self, db):
        day = 86400
        await db.cleanup(NOW, day, day, day, 10 * day, 5 * day)

    def test_is_configurable_and_positive(self):
        parsed = parse_rules_document({"global": {"actions_retention": "30d"}})
        assert parsed.global_rules.actions_retention == 30 * 86400
        with pytest.raises(ConfigError):
            parse_rules_document({"global": {"actions_retention": "0s"}})

    async def test_old_actions_and_their_dms_are_removed(self, db):
        old = await self.action(db, NOW - 11 * 86400)
        recent = await self.action(db, NOW - 9 * 86400)
        await db.add_action_dm(old.id, ADMIN_ID, 1)
        await db.add_action_dm(recent.id, ADMIN_ID, 2)
        await self.run_cleanup(db)
        assert await db.get_action(old.id) is None
        assert await db.action_dms(old.id) == []
        assert await db.get_action(recent.id) is not None
        assert await db.action_dms(recent.id) == [(ADMIN_ID, 2)]

    async def test_message_text_is_dropped_before_the_action(self, db):
        old = await self.action(db, NOW - 6 * 86400)
        fresh = await self.action(db, NOW - 4 * 86400)
        await self.run_cleanup(db)
        kept = await db.get_action(old.id)
        assert kept is not None
        assert kept.message_text == ""
        assert kept.reasons == old.reasons
        assert (await db.get_action(fresh.id)).message_text == "text"

    async def test_button_on_a_pruned_card_explains_itself(
        self, feed, db, session
    ):
        old = await self.action(db, NOW - 11 * 86400)
        await self.run_cleanup(db)
        data = ActionCb(action_id=old.id, verb="unban", surface=SURFACE_DM)
        await feed(
            callback_query=CallbackQuery(
                id="1",
                from_user=make_user(ADMIN_ID),
                chat_instance="x",
                data=data.pack(),
            )
        )
        assert "no longer exists" in session.calls(AnswerCallbackQuery)[0].text
        assert session.calls(UnbanChatMember) == []
