from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401 (registers the tables on Base.metadata)
from app.db import Base, engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            # SQLite rebuilds a table when altering it, and foreign-key checks block that during migrations
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # close the implicit transaction so Alembic opens and commits its own
        # render_as_batch: makes column changes work on SQLite too
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
