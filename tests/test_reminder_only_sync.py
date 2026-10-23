"""Reminder mode never resumes bulk conversations or historical approval jobs."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import ok
from sqlalchemy import func, select

from app.events import BackgroundSync, enqueue
from app.mention_sync import MentionSync
from app.models import (
    ApprovalInstance,
    ApprovalReview,
    ApprovalTask,
    Chat,
    EventInbox,
    Message,
    SyncJob,
    WorkerState,
    utcnow,
)
from app.worker import sync_mentions_once

pytestmark = pytest.mark.integration


def task(identifier, instance, *, topic="1", visible=True, status="1"):
    return ApprovalTask(
        task_id=identifier, topic=topic, instance_code=instance, title=identifier,
        status=status, initiator_name="", generation="test", visible=visible,
        raw={"task_id": identifier, "instance_code": instance, "user_id": "ou_owner", "status": status},
    )


def review(identifier, instance, *, submitted=True, owner="ou_owner"):
    return ApprovalReview(
        task_id=identifier, instance_code=instance, owner_open_id=owner,
        decision="eligible" if submitted else "needs_review",
        execution_status="unknown" if submitted else "awaiting_review",
        policy_version="test", fingerprint="f" * 64, reasons=[],
        reviewed_at=utcnow(),
        submitted_at=utcnow() if submitted else None,
    )


@pytest.fixture
def reminder(make_app, settings):
    settings.reminders_only = True
    settings.dms_sender_app_id, settings.dms_chat_id = "cli_dba", "oc_dba"
    app = make_app(lambda _: pytest.fail("Queue bookkeeping must not call remote APIs"))
    calls = []
    fake_sync = SimpleNamespace(run=lambda resource: calls.append(resource) or {"items": 0, "has_more": False})
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, fake_sync)
    worker.next_snapshot = worker.next_subscription = utcnow() + timedelta(days=1)
    return SimpleNamespace(app=app, settings=settings, sessions=app.state.sessions, worker=worker, calls=calls)


def queue_resources(reminder):
    with reminder.sessions() as db:
        return set(db.scalars(select(SyncJob.resource)))


def test_first_tick_cancels_bulk_queue_without_deleting_cached_data(reminder):
    with reminder.sessions.begin() as db:
        db.add(Chat(chat_id="oc_old", name="Saved chat", chat_mode="group", raw={}, generation="old"))
        db.add(Message(
            message_id="om_old", chat_id="oc_old", thread_id="omt_old", msg_type="text",
            sender_name="Other", text="saved content", create_time=1, raw={},
        ))
        db.add_all([
            task("pending", "pending"),
            task("done", "done", visible=False, status="2"),
            task("history", "history", topic="2", status="2"),
            task("unread", "unread", topic="17"),
            task("submitted", "submitted", visible=False),
            task("stale_review", "stale_review", visible=False),
            task("foreign", "foreign", visible=False),
            review("submitted", "submitted"),
            review("stale_review", "stale_review", submitted=False),
            review("foreign", "foreign", owner="ou_other"),
        ])
        for resource in (
            "chats", "messages:oc_old", "messages:oc_dba", "thread:omt_old",
            "approvals:1", "approvals:2", "approvals:17", "approvals:18", "dms_cards",
            "instance:pending", "instance:done", "instance:history", "instance:unread",
            "instance:submitted", "instance:stale_review", "instance:foreign", "unexpected",
        ):
            enqueue(db, resource)
    reminder.worker.tick()
    assert queue_resources(reminder) | set(reminder.calls) == {
        "approvals:1", "dms_cards", "instance:pending", "instance:submitted",
    }
    with reminder.sessions() as db:
        assert db.get(Chat, "oc_old").name == "Saved chat"
        assert db.get(Message, "om_old").text == "saved content"
        assert db.scalar(select(func.count()).select_from(ApprovalTask)) == 7


def test_snapshot_events_and_old_chat_lookups_cannot_restore_bulk_jobs(reminder):
    with reminder.sessions.begin() as db:
        db.add(Chat(chat_id="oc_old", name="Saved chat", chat_mode="group", raw={}, generation="old"))
        db.add(Message(
            message_id="om_old", chat_id="oc_old", thread_id="omt_old", msg_type="text",
            sender_name="Other", text="saved content", create_time=1, raw={},
        ))
        db.add_all([task("pending", "pending"), task("done", "done", topic="2", status="2")])
        for identifier, event_type, hints in (
            ("ordinary", "im.message.receive_v1", {"chat_id": "oc_old", "chat_lookup_pending": True}),
            ("unknown_chat", "im.message.receive_v1", {"chat_id": "oc_unseen"}),
            ("dba", "im.message.receive_v1", {"chat_id": "oc_dba"}),
            ("done_approval", "approval.task.status_changed_v4", {"instance_code": "done"}),
            ("pending_approval", "approval.task.status_changed_v4", {"instance_code": "pending"}),
        ):
            db.add(EventInbox(event_id=identifier, event_type=event_type, hints=hints))
    reminder.worker.schedule_snapshot()
    reminder.worker.drain_events()
    with reminder.sessions.begin() as db:
        assert reminder.worker.finish_chat_lookups(db) is False
    assert queue_resources(reminder) == {"approvals:1", "dms_cards", "instance:pending"}
    with reminder.sessions() as db:
        assert all(event.processed_at for event in db.scalars(select(EventInbox)))
        assert not db.get(EventInbox, "ordinary").hints.get("chat_lookup_pending")
    reminder.worker.schedule_snapshot()
    assert queue_resources(reminder) == {"approvals:1", "dms_cards", "instance:pending"}


def test_dispatch_allowlist_blocks_jobs_even_without_cleanup(reminder, monkeypatch):
    with reminder.sessions.begin() as db:
        for resource in ("chats", "messages:oc_old", "thread:omt_old", "approvals:2", "instance:orphan"):
            enqueue(db, resource)
    monkeypatch.setattr(reminder.worker, "cancel_disallowed_jobs", lambda: None)
    reminder.worker.run_one_job()
    assert reminder.calls == []
    assert len(queue_resources(reminder)) == 5


def test_finished_pending_task_stops_its_instance_job(reminder):
    with reminder.sessions.begin() as db:
        db.add(task("pending", "pending"))
        enqueue(db, "instance:pending")
    reminder.worker.cancel_disallowed_jobs()
    assert queue_resources(reminder) == {"instance:pending"}
    with reminder.sessions.begin() as db:
        db.get(ApprovalTask, ("pending", "1")).visible = False
    reminder.worker.run_one_job()
    assert reminder.calls == [] and not queue_resources(reminder)


def test_dms_uses_dedicated_job_instead_of_saved_history_cursor(reminder):
    with reminder.sessions.begin() as db:
        enqueue(db, "messages:oc_dba")
    reminder.worker.schedule_snapshot()
    for _ in range(2):
        reminder.worker.run_one_job()
    assert "dms_cards" in reminder.calls
    assert not any(resource.startswith(("messages:", "thread:")) for resource in reminder.calls)
    assert "messages:oc_dba" not in queue_resources(reminder)


def test_pending_approval_list_and_detail_still_refresh(make_app, settings):
    settings.reminders_only = True
    settings.dms_sender_app_id = settings.dms_chat_id = ""
    calls = []

    def remote(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/tasks"):
            return ok({"tasks": [{
                "task_id": "new_pending", "instance_code": "new_instance",
                "user_id": "ou_owner", "status": "1", "title": "New approval",
            }], "has_more": False})
        if request.url.path.endswith("/instances/detail"):
            return ok({"instance_code": "new_instance", "form": "[]"})
        pytest.fail("Reminder mode called an unrelated endpoint")

    app = make_app(remote)
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.schedule_snapshot()
    worker.run_one_job()
    worker.run_one_job()
    with app.state.sessions() as db:
        assert db.get(ApprovalTask, ("new_pending", "1")).visible
        assert db.get(ApprovalInstance, "new_instance") is not None
        assert not list(db.scalars(select(SyncJob.resource)))
    assert len(calls) == 2


def test_bulk_queue_shutdown_does_not_disable_independent_mentions(make_app, settings):
    settings.reminders_only = True
    now = utcnow()
    raw = {
        "message_id": "om_new", "chat_id": "oc_new", "msg_type": "text",
        "create_time": str(int((now - timedelta(seconds=1)).timestamp() * 1000)),
        "sender": {"id": "ou_other", "id_type": "open_id", "sender_type": "user"},
        "mentions": [{"id": "ou_owner", "id_type": "open_id", "key": "@_user_1"}],
        "body": {"content": '{"text":"@_user_1 new mention"}'},
    }

    def remote(request):
        if request.url.path.endswith("/messages/search"):
            return ok({"items": [{"meta_data": {"message_id": "om_new"}}], "has_more": False})
        if request.url.path.endswith("/messages/mget"):
            return ok({"items": [raw]})
        if request.url.path.endswith("/chats/oc_new"):
            return ok({"name": "New chat", "chat_mode": "group"})
        pytest.fail("A stopped bulk job performed a remote read")

    app = make_app(remote)
    with app.state.sessions.begin() as db:
        enqueue(db, "messages:oc_old")
        enqueue(db, "thread:omt_old")
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.run_one_job()
    reader = MentionSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    sync_mentions_once(app.state.sessions, reader)
    with app.state.sessions() as db:
        assert db.get(Message, "om_new") is not None
        assert db.get(WorkerState, "mentions").status == "synced"
        assert not list(db.scalars(select(SyncJob.resource)))
