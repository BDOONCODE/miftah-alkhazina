from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401 — يسجّل الجداول في Base.metadata
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
            # SQLite يعيد بناء الجدول عند تعديله، وفحص المفاتيح الأجنبية يمنع ذلك أثناء الترحيل
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # نقفل المعاملة الضمنية عشان Alembic يفتح معاملته ويثبّتها
        # render_as_batch: يخلي تعديل الأعمدة يشتغل على SQLite كذلك
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
