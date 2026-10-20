import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import healthcheck
from app.db import DatabaseNotReady
from app.models import WorkerState, utcnow


def test_healthcheck_import_does_not_load_sdk_or_background_services():
    # The test process already imports the app; isolate module loading in a new process.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.healthcheck; "
            "assert not any(name == 'lark_oapi' or name.startswith('lark_oapi.') "
            "for name in sys.modules); "
            "assert not {'app.worker', 'app.events', 'app.approval_review'} & sys.modules.keys()",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "age_seconds", "healthy"),
    [
        (None, 0, False),
        ("connected", 0, True),
        ("connected", 45, True),
        ("connected", 46, False),
        ("reconnecting", 10, True),
        ("stopped", 0, False),
    ],
)
def test_healthcheck_requires_current_schema_and_live_heartbeat(
    settings, database, monkeypatch, status, age_seconds, healthy
):
    now = utcnow()
    monkeypatch.setattr(healthcheck, "utcnow", lambda: now)
    if status is not None:
        with Session(database) as db, db.begin():
            db.add(
                WorkerState(
                    name="events",
                    status=status,
                    heartbeat_at=now - timedelta(seconds=age_seconds),
                )
            )
    if healthy:
        healthcheck.check_health(settings)
    else:
        with pytest.raises(SystemExit) as exc:
            healthcheck.check_health(settings)
        assert exc.value.code == 1


@pytest.mark.integration
def test_healthcheck_rejects_schema_mismatch_even_with_fresh_heartbeat(
    settings, database, monkeypatch
):
    with Session(database) as db, db.begin():
        db.add(WorkerState(name="events", status="connected", heartbeat_at=utcnow()))
    monkeypatch.setattr("app.db.expected_revisions", lambda: {"missing_migration"})
    with pytest.raises(DatabaseNotReady):
        healthcheck.check_health(settings)
