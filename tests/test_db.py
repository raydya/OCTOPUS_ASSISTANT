from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError

from app import db


class Engine:
    def __init__(self, revisions):
        self.revisions = revisions

    @contextmanager
    def connect(self):
        yield self

    def execute(self, statement):
        return None

    def scalars(self, statement):
        return self.revisions


def test_readiness_rejects_missing_migrations():
    with pytest.raises(db.DatabaseNotReady, match="schema is not current"):
        db.check_database(Engine(["0001"]))
    db.check_database(Engine(db.expected_revisions()))
    db.check_database(Engine([]), require_schema=False)


def test_wait_recovers_after_transient_disconnect(monkeypatch):
    attempts = []

    def probe(engine, *, require_schema):
        attempts.append(require_schema)
        if len(attempts) == 1:
            raise OperationalError("connection", None, Exception("private database detail"))

    monkeypatch.setattr(db, "check_database", probe)
    monkeypatch.setattr(db.time, "sleep", lambda _: None)
    db.wait_for_database(None)
    assert attempts == [True, True]


def test_wait_deadline_never_leaks_database_error(monkeypatch):
    clock = iter([0, 2])
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))

    def unavailable(*args, **kwargs):
        raise OperationalError("secret query", None, Exception("postgresql://private-password"))

    monkeypatch.setattr(db, "check_database", unavailable)
    with pytest.raises(db.DatabaseNotReady, match="startup deadline reached") as raised:
        db.wait_for_database(None, timeout=1)
    assert "private-password" not in str(raised.value)
    assert raised.value.__suppress_context__
