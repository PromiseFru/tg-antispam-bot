from __future__ import annotations

from pathlib import Path

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from conftest import CHAT_ID, TEST_DB_KEY
from tg_antispam_bot.db import Database, DatabaseKeyError

SECRET_TEXT = "very-identifiable-spam-text"
OTHER_KEY = "cd" * 32


@pytest.fixture
async def open_db():
    opened: list[Database] = []

    async def opener(path: Path | str, key: str = TEST_DB_KEY) -> Database:
        database = Database(str(path), key)
        await database.connect()
        opened.append(database)
        return database

    yield opener
    for database in opened:
        await database.close()


async def write_action(db: Database) -> None:
    await db.insert_action(
        chat_id=CHAT_ID,
        user_id=1,
        user_name="Name",
        score=1,
        reasons=[],
        message_text=SECRET_TEXT,
        action="delete",
        until_ts=None,
        deleted_count=0,
        errors=[],
        now=1,
    )


async def test_nothing_on_disk_is_plaintext_even_before_close(
    tmp_path: Path, open_db
):
    db = await open_db(tmp_path / "x.db")
    await write_action(db)
    files = list(tmp_path.iterdir())
    assert any(path.name.endswith("-wal") for path in files)
    for path in files:
        content = path.read_bytes()
        assert SECRET_TEXT.encode() not in content
        assert not content.startswith(b"SQLite format 3")


async def test_data_survives_reopen_with_same_key(tmp_path: Path, open_db):
    path = tmp_path / "x.db"
    db = await open_db(path)
    await write_action(db)
    await db.close()
    reopened = await open_db(path)
    assert (await reopened.recent_actions(CHAT_ID, 1))[
        0
    ].message_text == SECRET_TEXT


async def test_wrong_key_is_refused(tmp_path: Path, open_db):
    path = tmp_path / "x.db"
    db = await open_db(path)
    await write_action(db)
    await db.close()
    with pytest.raises(DatabaseKeyError):
        await Database(str(path), OTHER_KEY).connect()


async def test_existing_plaintext_database_is_refused(tmp_path: Path):
    path = tmp_path / "plain.db"
    plain = sqlcipher.connect(str(path))
    plain.execute("CREATE TABLE t (x)")
    plain.commit()
    plain.close()
    assert path.read_bytes().startswith(b"SQLite format 3")
    with pytest.raises(DatabaseKeyError):
        await Database(str(path), TEST_DB_KEY).connect()


@pytest.mark.parametrize(
    "key", ["", "short", "zz" * 32, "ab" * 32 + "'; DROP TABLE chats; --"]
)
def test_malformed_key_is_rejected_before_reaching_sql(
    tmp_path: Path, key: str
):
    with pytest.raises(DatabaseKeyError):
        Database(str(tmp_path / "x.db"), key)
