from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool
from sqlcipher3 import dbapi2 as sqlcipher

from tg_antispam_bot.config import DatabaseSettings
from tg_antispam_bot.db import open_encrypted
from tg_antispam_bot.models import Base


def run_on(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_from_command_line() -> None:
    settings = DatabaseSettings()
    engine = create_engine(
        "sqlite://",
        module=sqlcipher,
        poolclass=NullPool,
        creator=lambda: open_encrypted(
            settings.db_path, settings.db_encryption_key.get_secret_value()
        ),
    )
    with engine.begin() as connection:
        run_on(connection)
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "offline migrations are not supported for an encrypted database"
    )

provided = context.config.attributes.get("connection")
if provided is not None:
    run_on(provided)
else:
    run_from_command_line()
