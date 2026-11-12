"""Alembic environment. Uses the application's settings and declarative metadata."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import api.accounting.tables
import api.ai.spend_tables
import api.app.auth.tables
import api.execution.tables
import api.liquidity.tables
import api.workflow.tables  # noqa: F401
from api.config import get_settings
from api.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        get_settings().database_url,
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 5},  # fail fast instead of hanging
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
