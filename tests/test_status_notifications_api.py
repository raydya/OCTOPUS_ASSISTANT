import hashlib
from datetime import timedelta

import pytest
from conftest import login
from fastapi.testclient import TestClient

from app.models import StatusNotification, utcnow

pytestmark = pytest.mark.integration


def _notification(identifier, status, *, owner="ou_owner", created_at=None):
    now = created_at or utcnow()
    submitted = now + timedelta(seconds=1) if status in {"sent", "unknown"} else None
    return StatusNotification(
        id=identifier,
        source_key=hashlib.sha256(identifier.encode()).hexdigest(),
        owner_open_id=owner,
        revision=hashlib.sha256(f"revision:{identifier}".encode()).hexdigest(),
        phase="needs_human",
        text=f"审批需要你介入：{identifier}",
        status=status,
        submitted_at=submitted,
        sent_at=now + timedelta(seconds=2) if status == "sent" else None,
        message_id=f"om_{identifier}" if status == "sent" else None,
        error_code="status_notification_unavailable" if status == "unknown" else None,
        created_at=now,
        updated_at=now + timedelta(seconds=3),
    )


def _app_without_feishu(make_app):
    return make_app(lambda request: pytest.fail(
        f"Status audit reads must not call Feishu: {request.method} {request.url.path}"
    ))


def test_private_status_audit_only_lists_bound_owner_with_delivery_results(make_app):
    app = _app_without_feishu(make_app)
    now = utcnow()
    with app.state.sessions.begin() as db:
        db.add_all([
            _notification("owner_sent", "sent", created_at=now - timedelta(minutes=2)),
            _notification("owner_unknown", "unknown", created_at=now - timedelta(minutes=1)),
            _notification("foreign_sent", "sent", owner="ou_other", created_at=now),
        ])
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/api/status-notifications").status_code == 401
        assert client.get("/api/status").status_code == 401
        login(client)
        response = client.get("/api/status-notifications?limit=1")
        assert response.status_code == 200
        page = response.json()
        assert page["has_more"] is True
        assert [row["id"] for row in page["items"]] == ["owner_unknown"]
        unknown = page["items"][0]
        assert unknown["status"] == "unknown"
        assert unknown["phase"] == "needs_human"
        assert unknown["submitted_at"] is not None
        assert unknown["sent_at"] is None
        assert unknown["message_id"] is None
        assert unknown["error_code"] == "status_notification_unavailable"
        assert unknown["text"] == "审批需要你介入：owner_unknown"
        assert unknown["created_at"] is not None
        assert unknown["updated_at"] is not None

        page = client.get("/api/status-notifications?limit=1&offset=1").json()
        assert page["has_more"] is False
        assert [row["id"] for row in page["items"]] == ["owner_sent"]
        sent = page["items"][0]
        assert sent["status"] == "sent"
        assert sent["submitted_at"] is not None
        assert sent["sent_at"] is not None
        assert sent["message_id"] == "om_owner_sent"
        assert sent["error_code"] is None
        assert "foreign_sent" not in client.get("/api/status-notifications").text


@pytest.mark.parametrize("enabled", [True, False])
def test_status_exposes_notification_switch_and_delivery_counts(make_app, settings, enabled):
    settings.status_notifications_enabled = enabled
    app = _app_without_feishu(make_app)
    with app.state.sessions.begin() as db:
        db.add_all([
            _notification("sent", "sent"),
            _notification("unknown", "unknown"),
            _notification("queued", "queued"),
            _notification("permission", "blocked_permission"),
            _notification("superseded", "superseded"),
        ])
    with TestClient(app, base_url="http://localhost:8000") as client:
        login(client)
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["status_notifications"] == {
            "enabled": enabled, "sent": 1, "pending": 2, "unknown": 1,
        }
