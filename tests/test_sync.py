import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import expire_access, login, ok, token_response
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.models import Account, ApprovalTask, Chat, Message, SyncState

pytestmark = pytest.mark.integration


def chat(chat_id):
    return {"chat_id": chat_id, "name": chat_id, "chat_mode": "p2p"}


def seed_chat(app):
    with app.state.sessions.begin() as db:
        db.add(Chat(chat_id="oc_one", name="同事", chat_mode="p2p", raw={}, generation="seed"))


def test_pagination_resumes_deduplicates_and_does_not_erase_on_partial(make_app):
    calls = []

    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer user-access"
        assert request.url.path == "/open-apis/im/v1/chats"
        assert request.url.params["types"] == "p2p,group"
        cursor = request.url.params.get("page_token")
        calls.append(cursor)
        if cursor is None:
            return ok({"items": [chat("oc_one")], "has_more": True, "page_token": "next"})
        return ok(
            {
                "items": [chat("oc_one"), chat("oc_two")],
                "has_more": False,
                "page_token": "unused-last-token",
            }
        )

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(Chat(chat_id="oc_old", name="旧群", chat_mode="group", raw={}, generation="old"))
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/chats", json={}, headers=headers).json()["has_more"]
        with app.state.sessions() as db:
            assert db.get(Chat, "oc_old").visible
        assert not client.post("/api/sync/chats", json={}, headers=headers).json()["has_more"]
        assert calls == [None, "next"]
        assert len(client.get("/api/chats").json()["items"]) == 2
        with app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(Chat)) == 3
            assert not db.get(Chat, "oc_old").visible
            assert db.get(SyncState, "chats").page_token is None


def test_failed_page_rolls_back_and_can_be_retried_with_same_cursor(make_app):
    fail = True
    cursors = []

    def handler(request):
        cursor = request.url.params.get("page_token")
        cursors.append(cursor)
        if cursor is None:
            return ok({"items": [chat("oc_one")], "has_more": True, "page_token": "next"})
        if fail:
            return ok({"items": [chat("oc_two")], "has_more": True, "page_token": "next"})
        return ok({"items": [chat("oc_two")], "has_more": False})

    app = make_app(handler)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/chats", json={}, headers=headers).status_code == 200
        assert client.post("/api/sync/chats", json={}, headers=headers).status_code == 502
        with app.state.sessions() as db:
            assert db.get(Chat, "oc_two") is None
            assert db.get(SyncState, "chats").page_token == "next"
        fail = False
        assert client.post("/api/sync/chats", json={}, headers=headers).status_code == 200
        assert cursors == [None, "next", "next"]
        assert client.get("/api/sync-runs").json()["items"][1]["status"] == "failed"


def test_message_window_is_frozen_on_resume_and_recalled_body_is_removed(make_app):
    queries = []
    deleted = False

    def handler(request):
        queries.append(dict(request.url.params))
        raw = {
            "message_id": "om_one",
            "msg_type": "text",
            "create_time": "1700000000000",
            "sender": {"id": "ou_peer", "sender_name": "同事"},
            "deleted": deleted,
            "body": {"content": json.dumps({"text": "sensitive recalled text"})},
        }
        return ok({"items": [raw], "has_more": len(queries) == 1, "page_token": "next"})

    app = make_app(handler)
    seed_chat(app)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        client.post("/api/sync/messages/oc_one", json={"days": 7}, headers=headers)
        client.post("/api/sync/messages/oc_one", json={"days": 90}, headers=headers)
        assert queries[0]["start_time"] == queries[1]["start_time"]
        assert queries[0]["end_time"] == queries[1]["end_time"]
        assert int(queries[0]["end_time"]) - int(queries[0]["start_time"]) == 7 * 86400
        deleted = True
        client.post("/api/sync/messages/oc_one", json={"days": 30}, headers=headers)
        with app.state.sessions() as db:
            assert db.scalar(select(func.count()).select_from(Message)) == 1
            assert db.get(Message, "om_one").text == "消息已撤回"
            assert "sensitive recalled text" not in json.dumps(db.get(Message, "om_one").raw)


def test_approval_snapshot_removal_does_not_imply_approval(make_app):
    round_number = 0

    def handler(request):
        assert request.url.path == "/open-apis/approval/v4/tasks"
        assert "user_id" not in request.url.params
        task = {
            "task_id": "task_one",
            "instance_code": "instance_one",
            "status": "1",
            "title": "报销",
        }
        return ok({"tasks": [task] if round_number == 0 else [], "has_more": False, "count": 99})

    app = make_app(handler)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/approvals/1", json={}, headers=headers).status_code == 200
        assert client.get("/api/status").json()["counts"]["pending"] == 1
        round_number = 1
        assert client.post("/api/sync/approvals/1", json={}, headers=headers).status_code == 200
        assert client.get("/api/approvals").json()["items"] == []
        with app.state.sessions() as db:
            task = db.get(ApprovalTask, ("task_one", "1"))
            assert not task.visible
            assert task.status == "1"


def test_refreshed_token_is_saved_even_if_data_read_fails(make_app):
    grants = []

    def handler(request):
        if request.url.path.endswith("/oauth/v3/token"):
            grants.append(json.loads(request.content))
            return httpx.Response(200, json=token_response("rotated-access", "rotated-refresh"))
        assert request.headers["Authorization"] == "Bearer rotated-access"
        return httpx.Response(403, json={"code": 99991679})

    app = make_app(handler)
    expire_access(app)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/chats", json={}, headers=headers).status_code == 502
        assert grants[0]["refresh_token"] == "user-refresh"
        with app.state.sessions() as db:
            assert app.state.security.decrypt(db.get(Account, 1).refresh_token) == "rotated-refresh"
            assert db.get(SyncState, "chats") is None


def test_concurrent_refresh_only_rotates_once(make_app):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=token_response("new-access", "new-refresh"))

    app = make_app(handler)
    expire_access(app)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: app.state.feishu.access_token(), range(4)))
    assert results == ["new-access"] * 4
    assert len(calls) == 1


def test_parallel_sync_returns_busy_instead_of_corrupting_snapshot(make_app):
    def handler(request):
        pytest.fail("Contended sync must not call Feishu")

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(81024002)"))
        with TestClient(app, base_url="http://localhost:8000") as client:
            headers = login(client)
            response = client.post("/api/sync/chats", json={}, headers=headers)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "sync_busy"


def test_approval_detail_uses_user_endpoint_and_decodes_form(make_app):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer user-access"
        if request.url.path == "/open-apis/approval/v4/tasks":
            return ok(
                {
                    "tasks": [
                        {"task_id": "t1", "instance_code": "i1", "status": "1", "title": "设备申请"}
                    ],
                    "has_more": False,
                }
            )
        assert request.url.path == "/open-apis/approval/v4/instances/detail"
        assert request.url.params["instance_code"] == "i1"
        return ok(
            {
                "instance_code": "i1",
                "status": "PENDING",
                "form": '[{"name":"用途","value":"研发"}]',
                "current_nodes": [],
                "operation_records": [],
            }
        )

    app = make_app(handler)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/instances/unknown", headers=headers).status_code == 404
        assert client.post("/api/sync/approvals/1", json={}, headers=headers).status_code == 200
        assert client.post("/api/sync/instances/i1", headers=headers).status_code == 200
        detail = client.get("/api/approvals/i1").json()
        assert detail["data"]["form"] == [{"name": "用途", "value": "研发"}]
        assert detail["data"]["status"] == "PENDING"


def test_thread_reads_use_thread_container_and_keep_chat_association(make_app):
    def handler(request):
        assert request.url.params["container_id_type"] == "thread"
        assert request.url.params["container_id"] == "omt_thread"
        assert "start_time" not in request.url.params
        return ok(
            {
                "items": [
                    {
                        "message_id": "om_reply",
                        "parent_id": "om_root",
                        "msg_type": "text",
                        "body": {"content": '{"text":"回复"}'},
                        "create_time": "1700000000000",
                    }
                ],
                "has_more": False,
            }
        )

    app = make_app(handler)
    seed_chat(app)
    with app.state.sessions.begin() as db:
        db.add(
            Message(
                message_id="om_root",
                chat_id="oc_one",
                thread_id="omt_thread",
                msg_type="text",
                text="话题",
                sender_name="同事",
                create_time=1,
                raw={},
            )
        )
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/threads/unknown", json={}, headers=headers).status_code == 404
        assert (
            client.post("/api/sync/threads/omt_thread", json={}, headers=headers).status_code == 200
        )
        messages = client.get("/api/chats/oc_one/messages?thread_id=omt_thread").json()["items"]
        assert messages[0]["message_id"] == "om_reply"
        assert messages[0]["text"] == "回复"


def test_rejected_access_token_refreshes_once_and_replays_only_read(make_app):
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            assert json.loads(request.content)["grant_type"] == "refresh_token"
            return httpx.Response(200, json=token_response("fresh-access", "fresh-refresh"))
        if request.headers["Authorization"] == "Bearer user-access":
            return httpx.Response(400, json={"code": 99991668})
        assert request.headers["Authorization"] == "Bearer fresh-access"
        return ok({"items": [], "has_more": False})

    app = make_app(handler)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.post("/api/sync/chats", json={}, headers=headers).status_code == 200
        assert calls == ["GET", "POST", "GET"]
