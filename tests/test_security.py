import json
from datetime import timedelta

import httpx
import pytest
from conftest import login, ok, token_response
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Account, DeviceAuthorization, utcnow

pytestmark = pytest.mark.integration


def test_private_routes_csrf_and_origin_are_enforced(make_app):
    def never_called(request):
        pytest.fail("Unauthorized requests must not reach Feishu")

    app = make_app(never_called)
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/").status_code == 200
        for path in [
            "/api/status",
            "/api/chats",
            "/api/approvals",
            "/api/sync-runs",
            "/api/openapi.json",
        ]:
            assert client.get(path).status_code == 401
        assert client.post("/auth/login", json={"password": "wrong"}).status_code == 401
        headers = login(client)
        assert client.post("/api/sync/chats", json={}).status_code == 403
        assert (
            client.post(
                "/api/sync/chats", json={}, headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.post("/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/chats").status_code == 401
        assert not any("approve" in route.path or "reply" in route.path for route in app.routes)


def device_response():
    return httpx.Response(
        200,
        json={
            "device_code": "private-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://accounts.feishu.cn/oauth/device",
            "expires_in": 600,
            "interval": 5,
        },
    )


def allow_poll(app):
    with app.state.sessions.begin() as db:
        db.scalar(select(DeviceAuthorization)).next_poll_at = utcnow() - timedelta(seconds=1)


def test_device_authorization_browser_bound_throttled_and_encrypted(make_app):
    token_calls = []

    def handler(request):
        if request.url.path.endswith("/device_authorization"):
            assert request.headers["authorization"].startswith("Basic ")
            assert "redirect_uri" not in request.content.decode()
            return device_response()
        if request.url.path.endswith("/oauth/v3/token"):
            token_calls.append(json.loads(request.content))
            return httpx.Response(200, json=token_response())
        return ok({"open_id": "ou_owner", "name": "本人", "tenant_key": "tenant_test"})

    app = make_app(handler, connected=False)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.get("/auth/feishu/callback").status_code == 404
        assert client.post("/auth/feishu/start").status_code == 403
        start = client.post("/auth/feishu/start", headers=headers)
        assert start.json()["user_code"] == "ABCD-EFGH"
        assert "private-device-code" not in start.text
        with app.state.sessions() as db:
            grant = db.scalar(select(DeviceAuthorization))
            assert grant.device_code != "private-device-code"
            assert app.state.security.decrypt(grant.device_code) == "private-device-code"
        other = TestClient(app, base_url="http://localhost:8000")
        other_headers = login(other)
        assert other.post("/auth/feishu/poll", headers=other_headers).status_code == 409
        other.close()
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "pending"
        assert not token_calls
        allow_poll(app)
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "connected"
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "connected"
        assert len(token_calls) == 1
        assert token_calls[0]["device_code"] == "private-device-code"
        with app.state.sessions() as db:
            account = db.get(Account, 1)
            assert account.access_token != "user-access"
            assert app.state.security.decrypt(account.refresh_token) == "user-refresh"
            grant = db.scalar(select(DeviceAuthorization))
            assert grant.device_code is None and grant.exchanged_token is None
        assert "user-access" not in client.get("/api/status").text


@pytest.mark.parametrize(
    "provider_error,status",
    [
        ("authorization_pending", "pending"),
        ("slow_down", "pending"),
        ("access_denied", "denied"),
        ("expired_token", "expired"),
    ],
)
def test_device_pending_and_terminal_states(make_app, provider_error, status):
    def handler(request):
        if request.url.path.endswith("/device_authorization"):
            return device_response()
        return httpx.Response(
            400,
            json={
                "code": 20001,
                "error": provider_error,
                "error_description": "private upstream details",
            },
        )

    app = make_app(handler, connected=False)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        client.post("/auth/feishu/start", headers=headers)
        allow_poll(app)
        result = client.post("/auth/feishu/poll", headers=headers)
        assert result.json()["status"] == status
        assert "private upstream" not in result.text
        if provider_error == "slow_down":
            assert result.json()["interval"] == 10
        with app.state.sessions() as db:
            grant = db.scalar(select(DeviceAuthorization))
            assert (grant.device_code is None) == (status != "pending")


def test_device_expiry_and_account_switch_rejected(make_app):
    def handler(request):
        if request.url.path.endswith("/device_authorization"):
            return device_response()
        if request.url.path.endswith("/oauth/v3/token"):
            return httpx.Response(200, json=token_response())
        return ok({"open_id": "ou_other", "name": "别人", "tenant_key": "tenant_test"})

    app = make_app(handler)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        client.post("/auth/feishu/start", headers=headers)
        with app.state.sessions.begin() as db:
            db.scalar(select(DeviceAuthorization)).expires_at = utcnow() - timedelta(seconds=1)
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "expired"
        client.post("/auth/feishu/start", headers=headers)
        allow_poll(app)
        assert (
            client.post("/auth/feishu/poll", headers=headers).json()["error_code"]
            == "wrong_account"
        )
        with app.state.sessions() as db:
            assert db.get(Account, 1).open_id == "ou_owner"


def test_profile_retry_does_not_exchange_device_code_again(make_app):
    token_calls, profile_calls = [], []

    def handler(request):
        if request.url.path.endswith("/device_authorization"):
            return device_response()
        if request.url.path.endswith("/oauth/v3/token"):
            token_calls.append(1)
            return httpx.Response(200, json=token_response())
        profile_calls.append(1)
        if len(profile_calls) <= 3:
            return httpx.Response(503, json={"code": 0})
        return ok({"open_id": "ou_owner", "name": "本人", "tenant_key": "tenant_test"})

    app = make_app(handler, connected=False)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        client.post("/auth/feishu/start", headers=headers)
        allow_poll(app)
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "pending"
        with app.state.sessions() as db:
            grant = db.scalar(select(DeviceAuthorization))
            assert grant.device_code is None
            assert "user-access" not in grant.exchanged_token
        allow_poll(app)
        assert client.post("/auth/feishu/poll", headers=headers).json()["status"] == "connected"
        assert len(token_calls) == 1
