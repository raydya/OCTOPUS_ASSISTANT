from copy import deepcopy

import pytest
from conftest import login, ok
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_approval_review import sample_detail, sample_task
from test_model_review import FakeModel, model_result

from app.model_review import ModelReviewService
from app.models import ApprovalFeedback, ApprovalReview, utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
def feedback_app(make_app, settings):
    settings.approval_model_enabled = True
    state = {"task": sample_task(), "detail": sample_detail(), "calls": []}

    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path == "/open-apis/approval/v4/tasks":
            return ok({"tasks": [deepcopy(state["task"])], "has_more": False})
        if request.url.path == "/open-apis/approval/v4/instances/detail":
            return ok(deepcopy(state["detail"]))
        pytest.fail(f"Unexpected Feishu request: {request.method} {request.url.path}")

    app = make_app(handler)
    provider = FakeModel()
    provider.result = model_result(decision="needs_human", questions=["请确认用途。"])
    service = ModelReviewService(settings, app.state.sessions, provider)
    assessed = service.evaluate(state["task"], state["detail"], "ou_owner")
    with app.state.sessions.begin() as db:
        db.add(ApprovalReview(
            task_id="task_1", instance_code="instance_task_1", owner_open_id="ou_owner",
            decision="needs_review", execution_status="awaiting_review",
            policy_version="test-empty", fingerprint="0" * 64,
            reasons=["请确认用途。"], reviewed_at=utcnow(),
            assessment_id=assessed["assessment_id"],
            snapshot_hash=assessed["snapshot_hash"], model_review=assessed,
        ))
    body = {
        "assessment_id": assessed["assessment_id"], "snapshot_hash": assessed["snapshot_hash"],
        "action": "answer", "text": "已核验，仅用于隔离测试。",
    }
    return app, state, body


def test_feedback_requires_browser_session_csrf_and_same_origin(feedback_app):
    app, state, body = feedback_app
    endpoint = "/api/approval-reviews/task_1/respond"
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.post(endpoint, json=body).status_code == 401
        headers = login(client)
        assert client.post(
            endpoint, json=body, headers={"Origin": "http://localhost:8000"},
        ).status_code == 403
        assert client.post(
            endpoint, json=body, headers={**headers, "Origin": "http://evil.invalid"},
        ).status_code == 403
    assert state["calls"] == []
    with app.state.sessions() as db:
        assert db.scalar(select(ApprovalFeedback)) is None


def test_feedback_rejects_changed_live_snapshot_without_writing_feedback(feedback_app):
    app, state, body = feedback_app
    state["detail"]["definition_name"] = "已经变更的业务申请"
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/approval-reviews/task_1/respond", json={**body, "action": "approve"},
            headers=login(client),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_snapshot_changed"
    assert all(method == "GET" for method, _ in state["calls"])
    with app.state.sessions() as db:
        assert db.scalar(select(ApprovalFeedback)) is None
        assert db.get(ApprovalReview, "task_1").submitted_at is None


def test_valid_answer_is_audited_and_never_directly_posts_approval(feedback_app):
    app, state, body = feedback_app
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        response = client.post(
            "/api/approval-reviews/task_1/respond", json=body, headers=headers,
        )
        review = client.get("/api/approval-reviews/task_1").json()["data"]
    assert response.status_code == 200
    assert response.json()["data"]["action"] == "answer"
    assert review["model_review"]["feedback_pending"] is True
    assert review["execution_status"] == "awaiting_review"
    assert review["submitted_at"] is None
    assert len(state["calls"]) == 2
    assert all(method == "GET" for method, _ in state["calls"])
    with app.state.sessions() as db:
        row = db.scalar(select(ApprovalFeedback))
        assert row.text == body["text"] and row.snapshot_hash == body["snapshot_hash"]
