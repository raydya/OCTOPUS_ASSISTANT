from datetime import timedelta

import pytest
from conftest import login
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_approval_attention import approval_system as approval_system
from test_approval_attention import discover_human_approval
from test_approval_review import review_setup as review_setup
from test_model_review import setup_model as setup_model

from app.models import ApprovalFeedback, ApprovalReview, MessageAttention, utcnow

pytestmark = pytest.mark.integration


def record(message_id="om_attention", **changes):
    now = utcnow()
    return MessageAttention(**{
        "message_id": message_id,
        "owner_open_id": "ou_owner",
        "chat_id": "oc_work",
        "chat_name": "工作群",
        "text": "线上支付全部失败，请本人立即决定回滚。",
        "source_created_at": now - timedelta(minutes=16),
        "source_hash": "a" * 64,
        "due_at": now - timedelta(minutes=1),
        "status": "waiting",
        "reasons": ["需要本人及时作出决定。"],
        "model_result": {
            "importance": "high", "requires_response": True,
            "summary": "支付故障需要决定回滚。",
            "model": "gpt-6-sol", "reasoning_effort": "xhigh",
        },
        "assessment_cache": {"private_runtime_material": "not-for-the-workbench"},
        "created_at": now,
        "updated_at": now,
        **changes,
    })


def app_without_feishu_calls(make_app):
    return make_app(lambda request: pytest.fail(
        f"Attention list or acknowledgment must not call Feishu: {request.method} {request.url.path}"
    ))


def test_attention_list_and_ack_require_session_csrf_and_same_origin(make_app):
    app = app_without_feishu_calls(make_app)
    with app.state.sessions.begin() as db:
        db.add(record())
    endpoint = "/api/attention/om_attention/ack"
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/api/attention").status_code == 401
        assert client.post(endpoint).status_code == 401
        headers = login(client)
        assert client.post(
            endpoint, headers={"Origin": "http://localhost:8000"},
        ).status_code == 403
        assert client.post(
            endpoint, headers={**headers, "Origin": "http://evil.invalid"},
        ).status_code == 403
        assert client.post(
            endpoint,
            headers={"X-CSRF-Token": headers["X-CSRF-Token"], "Sec-Fetch-Site": "cross-site"},
        ).status_code == 403
        assert client.get("/api/attention").status_code == 200
    with app.state.sessions() as db:
        row = db.get(MessageAttention, "om_attention")
        assert row.acknowledged_at is None
        assert row.status == "waiting"


def test_attention_list_is_paginated_and_omits_internal_runtime_material(make_app):
    app = app_without_feishu_calls(make_app)
    now = utcnow()
    with app.state.sessions.begin() as db:
        db.add_all([
            record("om_older", updated_at=now - timedelta(minutes=1)),
            record("om_newer", updated_at=now),
        ])
    with TestClient(app, base_url="http://localhost:8000") as client:
        login(client)
        page = client.get("/api/attention?limit=1").json()
        assert page["has_more"] is True
        assert [row["message_id"] for row in page["items"]] == ["om_newer"]
        item = page["items"][0]
        assert item["can_acknowledge"] is True
        assert item["model_result"]["model"] == "gpt-6-sol"
        assert item["text"].startswith("线上支付")
        assert "assessment_cache" not in item
        assert "private_runtime_material" not in str(page)
        second = client.get("/api/attention?limit=1&offset=1").json()
        assert second["has_more"] is False
        assert [row["message_id"] for row in second["items"]] == ["om_older"]


def test_acknowledge_is_local_idempotent_and_stops_pending_reminders(make_app):
    app = app_without_feishu_calls(make_app)
    with app.state.sessions.begin() as db:
        db.add(record())
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        first = client.post("/api/attention/om_attention/ack", headers=headers)
        assert first.status_code == 200
        first_row = first.json()["data"]
        assert first_row["status"] == "acknowledged"
        assert first_row["acknowledged"] is True
        assert first_row["can_acknowledge"] is False
        assert first_row["phone_submitted_at"] is None
        second = client.post("/api/attention/om_attention/ack", headers=headers)
        assert second.status_code == 200
        assert second.json()["data"]["acknowledged_at"] == first_row["acknowledged_at"]
        listed = client.get("/api/attention").json()["items"][0]
        assert listed["acknowledged_at"] == first_row["acknowledged_at"]
    with app.state.sessions() as db:
        row = db.get(MessageAttention, "om_attention")
        assert row.acknowledged_at is not None
        assert row.reminder_submitted_at is None
        assert row.phone_submitted_at is None


@pytest.mark.parametrize("accepted", [False, True])
def test_ack_does_not_retract_or_erase_an_already_submitted_phone_request(make_app, accepted):
    app = app_without_feishu_calls(make_app)
    submitted_at = utcnow() - timedelta(seconds=20)
    accepted_at = submitted_at + timedelta(seconds=2) if accepted else None
    with app.state.sessions.begin() as db:
        db.add(record(
            status="phone_accepted" if accepted else "phone_unknown",
            reminder_uuid="stable-reminder-id",
            reminder_message_id="om_reminder",
            reminder_submitted_at=submitted_at - timedelta(seconds=2),
            phone_submitted_at=submitted_at,
            phone_accepted_at=accepted_at,
        ))
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/attention/om_attention/ack", headers=login(client),
        )
        assert response.status_code == 200
        row = response.json()["data"]
        assert row["acknowledged"] is True
        assert row["phone_submitted_at"] == submitted_at.isoformat()
        assert row["phone_accepted_at"] == (accepted_at.isoformat() if accepted else None)
        assert "无法撤回" in " ".join(row["reasons"])
    with app.state.sessions() as db:
        row = db.get(MessageAttention, "om_attention")
        assert row.phone_submitted_at == submitted_at
        assert row.phone_accepted_at == accepted_at
        assert row.reminder_message_id == "om_reminder"


def test_ack_refuses_unknown_records_and_records_of_another_account(make_app):
    app = app_without_feishu_calls(make_app)
    with app.state.sessions.begin() as db:
        db.add(record(owner_open_id="ou_another_account"))
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        for identifier in ("om_absent", "om_attention"):
            response = client.post(f"/api/attention/{identifier}/ack", headers=headers)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "attention_not_found"
    with app.state.sessions() as db:
        assert db.get(MessageAttention, "om_attention").acknowledged_at is None


def test_approval_notification_exposes_source_and_uses_existing_protected_ack(make_app):
    app = app_without_feishu_calls(make_app)
    identifier = "approval_" + "b" * 64
    with app.state.sessions.begin() as db:
        db.add(record(
            identifier, source_type="approval",
            approval_task_id="task_review", approval_instance_code="instance_review",
            approval_assessment_id="00000000-0000-0000-0000-000000000001",
            chat_id="", chat_name="测试资源申请",
            text="请提供验收结论。",
            model_result={
                "decision": "needs_human", "source": "approval",
                "questions": ["请提供验收结论。"],
                "model": "gpt-6-sol", "reasoning_effort": "xhigh",
            },
        ))
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        item = client.get("/api/attention").json()["items"][0]
        assert item["source_type"] == "approval"
        assert item["approval_task_id"] == "task_review"
        assert item["approval_instance_code"] == "instance_review"
        assert item["model_result"]["questions"] == ["请提供验收结论。"]
        endpoint = f"/api/attention/{identifier}/ack"
        assert client.post(endpoint).status_code == 403
        response = client.post(endpoint, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["acknowledged"] is True
        assert response.json()["data"]["source_type"] == "approval"
    with app.state.sessions() as db:
        row = db.get(MessageAttention, identifier)
        assert row.acknowledged_at is not None
        assert row.phone_submitted_at is None


def test_acknowledging_approval_reminder_does_not_approve_or_change_the_approval(
    approval_system, make_app,
):
    system = approval_system
    reminder = discover_human_approval(system)
    app = make_app(
        lambda _: pytest.fail("Acknowledgment must not submit an external request"),
        connected=False,
    )
    with system.sessions() as db:
        review = db.get(ApprovalReview, "task_1")
        before = (
            review.decision, review.execution_status, review.assessment_id,
            review.snapshot_hash, review.model_review, review.submitted_at,
        )
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            f"/api/attention/{reminder.message_id}/ack", headers=login(client),
        )
        assert response.status_code == 200
        item = response.json()["data"]
        assert item["source_type"] == "approval"
        assert item["approval_task_id"] == "task_1"
        assert item["acknowledged"] is True
    with system.sessions() as db:
        review = db.get(ApprovalReview, "task_1")
        after = (
            review.decision, review.execution_status, review.assessment_id,
            review.snapshot_hash, review.model_review, review.submitted_at,
        )
        assert before == after
        assert review.execution_status == "awaiting_review"
        assert db.scalar(select(func.count()).select_from(ApprovalFeedback)) == 0
        assert db.get(MessageAttention, reminder.message_id).acknowledged_at is not None
    assert not system.feishu.posts and not system.feishu.sent and not system.feishu.calls
