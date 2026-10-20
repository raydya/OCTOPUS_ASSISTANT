import time
from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.config import Settings


class DatabaseNotReady(RuntimeError):
    """A public error that never embeds connection URLs or server messages."""


def make_engine(url):
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=30000"},
        pool_timeout=3,
        pool_size=5,
        max_overflow=5,
    )


def make_database(settings: Settings):
    engine = make_engine(settings.database_url.get_secret_value())
    return engine, sessionmaker(engine, expire_on_commit=False)


@lru_cache(maxsize=1)
def expected_revisions():
    directory = Path(__file__).resolve().parents[1] / "migrations"
    return set(ScriptDirectory(str(directory)).get_heads())


def check_database(engine, *, require_schema=True):
    with engine.connect() as db:
        # Bound the probe itself, including waits behind migration DDL locks.
        db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        if require_schema:
            revisions = set(db.scalars(text("SELECT version_num FROM alembic_version")))
            if revisions != expected_revisions():
                raise DatabaseNotReady("database schema is not current; run the migration service")
        else:
            db.execute(text("SELECT 1"))


def wait_for_database(engine, *, timeout=60, require_schema=True):
    deadline = time.monotonic() + timeout
    while True:
        try:
            check_database(engine, require_schema=require_schema)
            return
        except (SQLAlchemyError, DatabaseNotReady):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DatabaseNotReady(
                    "database startup deadline reached; check connectivity and run migrations"
                ) from None
            time.sleep(min(1, remaining))
