"""Worker liveness probe without loading the Feishu SDK or background services."""

import sys

from app.config import Settings
from app.db import check_database, make_database
from app.models import WorkerState, utcnow


def check_health(settings: Settings | None = None):
    engine, sessions = make_database(settings if settings is not None else Settings())
    try:
        check_database(engine)
        with sessions() as db:
            state = db.get(WorkerState, "events")
            if (
                not state
                or state.status == "stopped"
                or (utcnow() - state.heartbeat_at).total_seconds() > 45
            ):
                raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        check_health()
    except Exception:
        print("worker health check failed", file=sys.stderr)
        raise SystemExit(1) from None
