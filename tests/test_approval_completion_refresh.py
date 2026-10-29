"""Finish notified approvals without restoring historical approval collection."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_reminder_only_sync import queue_resources, task
from test_reminder_only_sync import reminder as reminder

from app.events import BackgroundSync, enqueue
from app.models import ApprovalTask, StatusNoticeSource, StatusNotification, SyncJob, utcnow

pytestmark = pytest.mark.integration


def tracked_card(system, *, identifier="finished", age=timedelta(minutes=5)):
    created = utcnow() - age
    with system.sessions.begin() as db:
        row = task(identifier, identifier, visible=False)
        db.add(row)
        source = StatusNoticeSource(
            key=identifier, owner_open_id="ou_owner", source_type="approval", source_id=identifier,
            revision="a" * 64, payload={"phase": "responded"}, notify_changes=True,
            card_create_state="ready", card_message_id=f"om_card_{identifier}",
            created_at=created, updated_at=utcnow(),
        )
        db.add(source)
        notice = StatusNotification(
            id=str(uuid4()), source_key=identifier, owner_open_id="ou_owner", revision="a" * 64,
            phase="received", text="approval", delivery_action="create", status="sent",
            message_id=source.card_message_id, submitted_at=created, sent_at=created,
            created_at=created, updated_at=created,
        )
        db.add(notice)
    return identifier, notice.id


def test_recent_notified_task_gets_final_instance_read_after_leaving_pending(reminder):
    tracked_card(reminder)
    reminder.worker.schedule_snapshot()

    assert "instance:finished" in queue_resources(reminder)
    assert not any(resource.startswith(("messages:", "thread:")) for resource in queue_resources(reminder))
    assert not queue_resources(reminder) & {"chats", "approvals:2", "approvals:17", "approvals:18"}

    # Restart reconstructs the allowlist from the persisted notification anchor.
    worker = BackgroundSync(reminder.settings, reminder.sessions, reminder.app.state.feishu, reminder.worker.sync)
    with reminder.sessions.begin() as db:
        for resource in ("approvals:1", "dms_cards"):
            db.delete(db.get(SyncJob, resource))
    worker.run_one_job()
    assert reminder.calls == ["instance:finished"]
    assert not queue_resources(reminder)


@pytest.mark.parametrize("phase", ["approved", "rejected", "transferred", "completed", "processed"])
def test_confirmed_completion_stops_targeted_refresh(reminder, phase):
    identifier, _ = tracked_card(reminder)
    reminder.worker.schedule_snapshot()
    assert f"instance:{identifier}" in queue_resources(reminder)
    with reminder.sessions.begin() as db:
        source = db.get(StatusNoticeSource, identifier)
        source.payload = {"phase": phase, "terminal": True}
    reminder.worker.cancel_disallowed_jobs()
    assert f"instance:{identifier}" not in queue_resources(reminder)
    reminder.worker.schedule_snapshot()
    assert f"instance:{identifier}" not in queue_resources(reminder)


@pytest.mark.parametrize("change", [
    "old_card", "legacy", "missing_anchor", "unknown_create", "foreign_source",
    "foreign_notice", "foreign_task", "wrong_notice_message", "wrong_task_identity",
    "wrong_instance_identity", "wrong_source_type", "wrong_phase", "unconfirmed_create",
])
def test_unrelated_or_unverifiable_history_is_not_finally_polled(reminder, change):
    from app.models import ApprovalTask

    identifier, notice_id = tracked_card(reminder)
    with reminder.sessions.begin() as db:
        source, notice = db.get(StatusNoticeSource, identifier), db.get(StatusNotification, notice_id)
        approval = db.get(ApprovalTask, (identifier, "1"))
        if change == "old_card":
            notice.sent_at = utcnow() - timedelta(days=14, minutes=1)
        elif change == "legacy":
            notice.delivery_action = "legacy"
        elif change == "missing_anchor":
            source.card_message_id = None
        elif change == "unknown_create":
            source.card_create_state = "unknown"
        elif change == "foreign_source":
            source.owner_open_id = "ou_other"
        elif change == "foreign_notice":
            notice.owner_open_id = "ou_other"
        elif change == "foreign_task":
            approval.raw = {**approval.raw, "user_id": "ou_other"}
        elif change == "wrong_notice_message":
            notice.message_id = "om_unrelated"
        elif change == "wrong_task_identity":
            approval.raw = {**approval.raw, "task_id": "other"}
        elif change == "wrong_instance_identity":
            approval.raw = {**approval.raw, "instance_code": "other"}
        elif change == "wrong_source_type":
            source.source_type = "message"
        elif change == "wrong_phase":
            source.payload = {"phase": "approved"}
        elif change == "unconfirmed_create":
            notice.status, notice.sent_at = "unknown", None
        enqueue(db, f"instance:{identifier}")
    reminder.worker.schedule_snapshot()
    assert f"instance:{identifier}" not in queue_resources(reminder)
    with reminder.sessions() as db:
        assert list(db.scalars(reminder.worker.reminder_instance_codes())) == []


def test_final_refresh_does_not_require_any_new_history_or_approval_submission(reminder):
    identifier, _ = tracked_card(reminder)
    with reminder.sessions.begin() as db:
        enqueue(db, f"instance:{identifier}")
        for resource in ("approvals:2", "messages:oc_old", "thread:omt_old", "instance:old_untracked"):
            enqueue(db, resource)
    reminder.worker.run_one_job()
    assert reminder.calls == [f"instance:{identifier}"]
    assert not queue_resources(reminder)
    with reminder.sessions() as db:
        from app.models import ApprovalReview

        assert not list(db.scalars(select(ApprovalReview)))


@pytest.mark.parametrize("markers", [
    {"instance_external_id": "people_instance", "task_external_id": "people_task"},
    {"instance_external_id": "people_instance"},
    {"task_external_id": "people_task"},
])
def test_active_external_approval_does_not_trigger_native_or_handled_detail_poll(reminder, markers):
    with reminder.sessions.begin() as db:
        row = task("external", "external")
        row.raw = {**row.raw, **markers}
        db.add(row)
        enqueue(db, "instance:external")
    reminder.worker.schedule_snapshot()
    assert queue_resources(reminder) == {"approvals:1", "dms_cards"}
    with reminder.sessions() as db:
        assert list(db.scalars(reminder.worker.reminder_instance_codes())) == []


@pytest.mark.parametrize("markers", [
    {}, {"instance_external_id": None, "task_external_id": None},
    {"instance_external_id": "", "task_external_id": ""},
    {"instance_external_id": None, "task_external_id": ""},
])
def test_native_pending_approval_keeps_detail_refresh_when_external_ids_are_empty(reminder, markers):
    with reminder.sessions.begin() as db:
        row = task("native", "native")
        row.raw = {**row.raw, **markers}
        db.add(row)
    reminder.worker.schedule_snapshot()
    assert queue_resources(reminder) == {"approvals:1", "dms_cards", "instance:native"}


def test_external_completion_is_only_polled_for_an_existing_delivered_card(reminder):
    identifier, _ = tracked_card(reminder, identifier="external_finished")
    with reminder.sessions.begin() as db:
        tracked = db.get(ApprovalTask, (identifier, "1"))
        tracked.raw = {
            **tracked.raw, "instance_external_id": "people_tracked", "task_external_id": "task_tracked",
        }
        untracked = task("external_untracked", "external_untracked", visible=False)
        untracked.raw = {
            **untracked.raw, "instance_external_id": "people_untracked", "task_external_id": "task_untracked",
        }
        db.add(untracked)
        enqueue(db, "instance:external_untracked")
        enqueue(db, "approvals:2")
    reminder.worker.schedule_snapshot()
    assert queue_resources(reminder) == {"approvals:1", "dms_cards", f"instance:{identifier}"}

    # A rollback/reopened pending observation must stop completion polling too.
    with reminder.sessions.begin() as db:
        db.get(ApprovalTask, (identifier, "1")).visible = True
    reminder.worker.cancel_disallowed_jobs()
    assert queue_resources(reminder) == {"approvals:1", "dms_cards"}
