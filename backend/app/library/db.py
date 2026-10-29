"""SQLite engine and transactions for the experience library.

The library has its own declarative base and its own SQLite file. Nothing
here imports app.database, so creating a library engine can never touch the
legacy RoleRadar database.
"""
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.library.errors import StorageError


class LibraryBase(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Stores aware datetimes as naive UTC and returns them as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None

        if value.tzinfo is None:
            raise ValueError("library timestamps must be timezone-aware")

        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None

        return value.replace(tzinfo=timezone.utc)


def make_engine(db_path: Path) -> Engine:
    engine = create_engine(
        URL.create("sqlite", database=str(db_path)),
        connect_args={"check_same_thread": False, "timeout": 10},
        # Keep bound values (user content) out of error messages and logs.
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, _record):
        # Let SQLAlchemy, not the sqlite3 driver, decide how transactions begin.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        # Overwrite removed rows with zeros instead of leaving them in free pages.
        cursor.execute("PRAGMA secure_delete = ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        # Take the write lock up front, so a read-check-write sequence (a
        # revision check, or a purge job re-checking its target) can never
        # interleave with another writer.
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        """Run one transaction: commit on success, roll back on any error."""
        session = self._sessions()

        try:
            with session.begin():
                yield session
        except SQLAlchemyError as error:
            raise StorageError("database operation failed") from error
        finally:
            session.close()
