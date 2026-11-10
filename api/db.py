"""Database engine and declarative base (PostgreSQL via SQLAlchemy 2)."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase

from api.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM tables. Alembic autogenerates from `Base.metadata`."""


@lru_cache
def get_engine() -> Engine:
    # Short connect timeout so a stopped database fails the readiness check quickly.
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def check_database() -> str:
    """Run a trivial query; return the server version string. Raises on failure."""
    with get_engine().connect() as conn:
        return str(conn.execute(text("SHOW server_version")).scalar_one())
