import json

import httpx
import pytest
from conftest import login, ok
from fastapi.testclient import TestClient

from app.errors import AppError, FeishuError
from app.models import Account

pytestmark = pytest.mark.integration


def test_approval_write_uses_user_endpoint_and_refuses_missing_scope(make_app):
    calls = []

    def handler(request):
        calls.append(request)
        return ok({})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        account = db.get(Account, 1)
        account.scope = "approval:task:read"
    with pytest.raises(AppError) as exc:
        app.state.feishu.approve_task("instance", "task", "同意")
    assert exc.value.code == "approval_write_scope_required"
    assert not calls
    with app.state.sessions.begin() as db:
        db.get(Account, 1).scope += " approval:task:write"
    app.state.feishu.approve_task("instance", "task", "同意")
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/open-apis/approval/v4/tasks/pass"
    assert calls[0].headers["Authorization"] == "Bearer user-access"
    assert json.loads(calls[0].content) == {
        "instance_code": "instance",
        "task_id": "task",
        "comment": "同意",
    }


@pytest.mark.parametrize("failure", ["timeout", "token_expired", "rate_limit", "server_error"])
def test_approval_writes_are_never_replayed(make_app, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if failure == "token_expired":
            return httpx.Response(200, json={"code": 99991677})
        if failure == "rate_limit":
            return httpx.Response(429, json={"code": 99991400})
        return httpx.Response(503, json={"code": 99999999})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.get(Account, 1).scope += " approval:task:write"
    with pytest.raises(FeishuError):
        app.state.feishu.approve_task("instance", "task", "同意")
    assert len(calls) == 1


def test_review_api_is_private_and_status_exposes_missing_write_authorization(make_app):
    app = make_app(lambda _: ok({}))
    with app.state.sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:read approval:instance:read"
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/api/approval-reviews").status_code == 401
        assert client.get("/api/approval-reviews/task").status_code == 401
        login(client)
        assert client.get("/api/approval-reviews").json() == {"items": [], "has_more": False}
        assert client.get("/api/approval-reviews/task").json() == {"data": None}
        automation = client.get("/api/status").json()["approval_automation"]
        assert automation["enabled"] is True
        assert automation["rule_count"] == 0
        assert automation["write_scope_granted"] is False
