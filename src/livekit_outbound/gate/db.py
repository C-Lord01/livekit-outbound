"""Database engine and session setup for the policy gate.

The gate keeps its contact history, suppression flags, and audit trail in a
SQLite database. By default that is ``./data/gate.db`` under the current
working directory; set ``GATE_DATABASE_URL`` to any SQLAlchemy URL to point
it elsewhere. The test suite builds its own in-memory engine and never
touches the default file.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

DATABASE_URL_ENV = "GATE_DATABASE_URL"
DEFAULT_DATABASE_PATH = Path("data") / "gate.db"


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models (2.0 style)."""


class UTCDateTime(TypeDecorator):
    """A DateTime that always stores and returns timezone-aware UTC values.

    SQLite has no native timezone-aware timestamp type, so plain
    ``DateTime(timezone=True)`` silently drops tzinfo on read (SQLAlchemy
    just returns a naive datetime). Every timestamp in this schema is
    required to be UTC, so this type rejects naive input on write and
    reattaches ``timezone.utc`` on read, making the round trip lossless.
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime passed to a UTC timestamp column; "
                "all timestamps must be timezone-aware"
            )
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """Enforce FK constraints on SQLite, which ignores them by default."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def database_url() -> str:
    """The configured database URL, defaulting to a file under ``./data``.

    The default directory is created on demand so a first run of the dial
    script or the seed script does not fail on a missing folder.
    """
    configured = os.getenv(DATABASE_URL_ENV, "").strip()
    if configured:
        return configured
    DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"


def create_gate_engine(url: str | None = None) -> Engine:
    """Create an engine for ``url`` (default: :func:`database_url`).

    ``check_same_thread=False`` lets a SQLite connection be used from a
    different thread than the one that opened it, which matters when the
    gate is called from inside an event loop or a threadpool.
    """
    return create_engine(
        url or database_url(),
        connect_args={"check_same_thread": False},
        future=True,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def open_session(url: str | None = None, *, create_schema: bool = True) -> Session:
    """Open a session on the configured database, creating tables if asked.

    Callers own the returned session and must close it.
    """
    engine = create_gate_engine(url)
    if create_schema:
        Base.metadata.create_all(bind=engine)
    return session_factory(engine)()
