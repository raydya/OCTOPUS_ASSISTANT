import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import ok
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from sqlalchemy import func, select

from app.events import BackgroundSync, EventInboxHandler, enqueue
from app.models import Chat, EventInbox, Message, SyncJob, utcnow
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
