import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import ok
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from sqlalchemy import func, select

from app.events import BackgroundSync, EventInboxHandler, enqueue
from app.models import (
    ApprovalInstance,
    ApprovalTask,
    Chat,
    EventInbox,
    Message,
    SyncJob,
    SyncState,
    utcnow,
)
from app.worker import FeishuSocket

pytestmark = pytest.mark.integration


def event_payload(event_id="evt_1", event_type="im.message.receive_v1", **overrides):
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "app_id": "cli_test",
                "tenant_key": "tenant_test",
                "event_id": event_id,
                "event_type": event_type,
                **overrides,
            },
            "event": {
                "message": {"chat_id": "oc_mine", "content": "private bot message"},
                "instance_code": "instance_1",
            },
        }
    ).encode()


def frame(payload, total=1, seq=0):
    result = Frame(SeqID=0, LogID=0, service=1, method=1, payload=payload)
    for key, value in {
        "message_id": "frame_id",
        "trace_id": "trace",
        "sum": str(total),
        "seq": str(seq),
        "type": "event",
    }.items():
        result.headers.add(key=key, value=value)
    return result


def test_sdk_ack_after_commit_dedup_and_fragment_reassembly(make_app, settings):
    app = make_app(lambda _: pytest.fail("Event ingestion must not call remote data APIs"))
    inbox = EventInboxHandler(settings, app.state.sessions)
    acknowledgements = []

    async def capture(data):
        with app.state.sessions() as db:
            assert db.get(EventInbox, "evt_1")  # Already committed when SDK acknowledges.
        ack = Frame()
        ack.ParseFromString(data)
        acknowledgements.append(json.loads(ack.payload)["code"])

    raw = event_payload()

    async def exercise():
        socket = FeishuSocket(settings, app.state.feishu, inbox)
        socket._write_message = capture
        await socket._handle_data_frame(frame(raw[:30], 2, 0))
        assert not acknowledgements
        await socket._handle_data_frame(frame(raw[30:], 2, 1))
        await socket._handle_data_frame(frame(raw))

    asyncio.run(exercise())
    assert acknowledgements == [200, 200]
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(EventInbox)) == 1
        assert db.get(EventInbox, "evt_1").hints == {"chat_id": "oc_mine"}


def test_sdk_returns_failure_when_event_cannot_be_persisted(make_app, settings, monkeypatch):
    app = make_app(lambda _: ok({}))
    inbox = EventInboxHandler(settings, app.state.sessions)

    def fail(_):
        raise RuntimeError("database_unavailable")

    monkeypatch.setattr(inbox, "_do_without_validation", fail)

    async def exercise():
        socket = FeishuSocket(settings, app.state.feishu, inbox)
        socket._write_message = AsyncMock()
        await socket._handle_data_frame(frame(event_payload()))
        ack = Frame()
        ack.ParseFromString(socket._write_message.call_args.args[0])
        assert json.loads(ack.payload)["code"] == 500

    asyncio.run(exercise())


def test_events_never_import_bot_only_content_or_other_tenants(make_app, settings):
    app = make_app(lambda _: pytest.fail("No tenant-data reads allowed"))
    inbox = EventInboxHandler(settings, app.state.sessions)
    inbox._do_without_validation(event_payload("wrong_app", app_id="other_app"))
    inbox._do_without_validation(event_payload("wrong_tenant", tenant_key="other_tenant"))
    inbox._do_without_validation(event_payload())
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.drain_events()
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(EventInbox)) == 1
        assert db.get(SyncJob, "chats")
        assert not db.get(SyncJob, "messages:oc_mine")
        assert db.scalar(select(func.count()).select_from(Message)) == 0


def test_approval_events_queue_all_user_task_topics(make_app, settings):
    app = make_app(lambda _: ok({}))
    EventInboxHandler(settings, app.state.sessions)._do_without_validation(
        event_payload(event_type="approval.task.status_changed_v4")
    )
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.drain_events()
    worker.drain_events()
    with app.state.sessions() as db:
        assert set(db.scalars(select(SyncJob.resource))) == {
            f"approvals:{topic}" for topic in ("1", "2", "17", "18")
        }
        assert db.get(EventInbox, "evt_1").processed_at


def test_job_retry_survives_restart_and_new_signal_during_sync(make_app, settings):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer user-access"
        if len(requests) == 1:
            # A new event during the read must not disappear when this job finishes.
            with app.state.sessions.begin() as db:
                enqueue(db, "chats", changed=True)
        return ok({"items": [], "has_more": False})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        enqueue(db, "chats")
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.run_one_job()
    with app.state.sessions.begin() as db:
        job = db.get(SyncJob, "chats")
        assert job
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
    restarted = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    restarted.run_one_job()
    with app.state.sessions() as db:
        assert not db.get(SyncJob, "chats")
    assert len(requests) == 2


def test_periodic_personal_sync_does_not_require_bot_events(make_app, settings):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/subscription"):
            assert json.loads(request.content) == {"subscription_type": "INVOLVED_APPROVAL"}
            return ok({})
        if request.url.path.endswith("/chats"):
            return ok(
                {
                    "items": [{"chat_id": "oc_personal", "name": "我的会话", "chat_mode": "p2p"}],
                    "has_more": False,
                }
            )
        if request.url.path.endswith("/messages"):
            return ok(
                {
                    "items": [
                        {
                            "message_id": "om_personal",
                            "msg_type": "text",
                            "body": {"content": '{"text":"我的个人聊天"}'},
                        }
                    ],
                    "has_more": False,
                }
            )
        return ok({"tasks": [], "has_more": False})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(
            Chat(
                chat_id="oc_personal",
                name="我的会话",
                chat_mode="p2p",
                raw={},
                generation="test",
                visible=True,
            )
        )
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.tick()
    with app.state.sessions() as db:
        assert db.get(SyncJob, "messages:oc_personal")
    for _ in range(5):
        worker.run_one_job()
    with app.state.sessions() as db:
        assert db.get(Message, "om_personal").text == "我的个人聊天"
    assert all(request.headers["authorization"] == "Bearer user-access" for request in calls)
    assert len([r for r in calls if r.url.path.endswith("/subscription")]) == 2


def test_live_work_bypasses_history_and_unknown_chat_gets_one_fresh_lookup(make_app, settings):
    calls = []
    pending = {"task_id": "task_pending", "instance_code": "i_pending", "status": "1"}

    def chat(chat_id):
        return {"chat_id": chat_id, "name": chat_id, "chat_mode": "p2p"}

    def handler(request):
        assert request.headers["authorization"] == "Bearer user-access"
        if request.url.path.endswith("/chats"):
            cursor = request.url.params.get("page_token")
            calls.append(("chats", cursor))
            if cursor == "old_first":
                return ok(
                    {"items": [chat("oc_history_0")], "has_more": True, "page_token": "old_last"}
                )
            if cursor == "old_last":
                return ok({"items": [], "has_more": False})
            return ok({"items": [chat("oc_history_0"), chat("oc_mine")], "has_more": False})
        if request.url.path.endswith("/tasks"):
            topic = request.url.params["topic"]
            calls.append(("approvals", topic))
            return ok({"tasks": [pending] if topic == "1" else [], "has_more": False})
        if request.url.path.endswith("/messages"):
            chat_id = request.url.params["container_id"]
            calls.append(("messages", chat_id))
            return ok(
                {
                    "items": [
                        {
                            "message_id": "om_live",
                            "msg_type": "text",
                            "body": {"content": '{"text":"new personal message"}'},
                        }
                    ]
                    if chat_id == "oc_mine"
                    else [],
                    "has_more": False,
                }
            )
        code = request.url.params["instance_code"]
        calls.append(("instance", code))
        assert code == "i_pending", "Historical details must not delay pending approvals"
        return ok({"instance_code": code, "form": "[]"})

    app = make_app(handler)
    now = utcnow()
    with app.state.sessions.begin() as db:
        for index in range(50):
            chat_id = f"oc_history_{index}"
            db.add(Chat(**chat(chat_id), raw={}, generation="old"))
            db.add(
                SyncJob(
                    resource=f"messages:{chat_id}",
                    next_attempt_at=now - timedelta(minutes=5) + timedelta(seconds=index),
                )
            )
            db.add(
                SyncJob(
                    resource=f"instance:history_{index}",
                    next_attempt_at=now - timedelta(minutes=4) + timedelta(seconds=index),
                )
            )
        db.add(Chat(**chat("oc_retry"), raw={}, generation="old"))
        db.add(
            SyncJob(
                resource="messages:oc_retry",
                next_attempt_at=now + timedelta(minutes=5),
                attempts=2,
                error_code="feishu_network",
            )
        )
        db.add(
            ApprovalTask(
                **pending, topic="1", title="pending", initiator_name="", raw={}, generation="old"
            )
        )
        db.add(SyncJob(resource="instance:i_pending", next_attempt_at=now))
        db.add(
            SyncState(
                resource="chats",
                generation="old",
                parameters={},
                page_token="old_first",
                has_more=True,
            )
        )
        for resource in ["chats", "approvals:1", "approvals:2", "approvals:17", "approvals:18"]:
            db.add(SyncJob(resource=resource, next_attempt_at=now - timedelta(minutes=1)))

    inbox = EventInboxHandler(settings, app.state.sessions)
    for event_id, chat_id in [
        ("new_chat", "oc_mine"),
        ("bot_only", "oc_bot"),
        ("retry", "oc_retry"),
    ]:
        payload = json.loads(event_payload(event_id))
        payload["event"]["message"]["chat_id"] = chat_id
        inbox._do_without_validation(json.dumps(payload).encode())
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.drain_events()

    for _ in range(3):
        with app.state.sessions.begin() as db:
            # Advance only the short page-continuation delay; no sleeping in the test.
            db.get(SyncJob, "chats").next_attempt_at = now - timedelta(minutes=2)
        worker.run_one_job()
    assert calls == [("chats", "old_first"), ("chats", "old_last"), ("chats", None)]
    with app.state.sessions() as db:
        assert db.get(SyncJob, "chats") is None  # Invisible bot chat does not loop forever.
        assert not db.get(EventInbox, "bot_only").hints.get("chat_lookup_pending")

    for _ in range(4):
        worker.run_one_job()
    assert {topic for kind, topic in calls[3:]} == {"1", "2", "17", "18"}
    assert all(kind == "approvals" for kind, _ in calls[3:])
    worker.run_one_job()
    worker.run_one_job()
    assert calls[-2:] == [("messages", "oc_mine"), ("instance", "i_pending")]
    worker.run_one_job()
    assert calls[-1] == ("messages", "oc_history_0")  # Ordinary work retains oldest-first order.
    with app.state.sessions() as db:
        assert db.get(Message, "om_live").text == "new personal message"
        assert db.get(ApprovalInstance, "i_pending")
        retry = db.get(SyncJob, "messages:oc_retry")
        assert retry.next_attempt_at == now + timedelta(minutes=5)
        assert retry.attempts == 2  # Recent events never bypass failed-job backoff.
