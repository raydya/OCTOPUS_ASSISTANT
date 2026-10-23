"""Late message synchronization must not masquerade as new owner notifications.

Exercise both real worker loops with isolated PostgreSQL and fake outbound APIs.
No production messages, model requests, or phone calls are made.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_attention import FakeFeishu, FakeModel
from test_status_notifications import (
    activate,
    add_attention,
    add_dms_attention,
    add_mention,
    add_pending,
    notice_rows,
    sent_phases,
)
from test_status_notifications import notices as notices

from app import attention
from app.attention import AttentionService
from app.models import Message, MessageAttention, StatusNoticeSource
from app.status_notifications import StatusNotificationService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("observer_first", [True, False], ids=["receipt_then_expired", "expired_only"])
def test_yesterdays_late_mention_does_not_send_new_receipt_or_restart_dm(
    notices, monkeypatch, observer_first,
):
    monkeypatch.setattr(attention, "utcnow", lambda: notices.clock.now)
    notices.settings.attention_enabled = True
    activate(notices)
    phone, model = FakeFeishu(), FakeModel()
    original_worker = AttentionService(notices.settings, notices.sessions, phone, model)
    original_worker.tick()  # Both notification services were enabled yesterday.
    message_created = notices.clock.now + timedelta(minutes=1)

    # The remote message was sent yesterday but its first local sync occurs
    # today. Preserve its original timestamp, as SyncService does in production.
    notices.clock.now += timedelta(days=1)
    add_mention(notices, created=message_created)
    recovered_worker = AttentionService(notices.settings, notices.sessions, phone, model)
    if observer_first:
        notices.service.tick()
    recovered_worker.tick()
    notices.service.tick()

    with notices.sessions() as db:
        tracked = db.get(MessageAttention, "om_mention")
        assert tracked.status == "skipped_restart"
    assert not phone.sent and not phone.calls and not model.calls
    bad_notices = [
        text for text, _, _ in notices.feishu.sent
        if "已收到，等待判断" in text or "服务恢复时已超过提醒期限" in text
    ]
    assert not bad_notices, "A day-old late sync generated new-receipt/restart status DMs"


def test_fresh_mention_still_sends_receipt_then_actual_judgment(notices, monkeypatch):
    monkeypatch.setattr(attention, "utcnow", lambda: notices.clock.now)
    notices.settings.attention_enabled = True
    activate(notices)
    phone, model = FakeFeishu(), FakeModel()
    worker = AttentionService(notices.settings, notices.sessions, phone, model)
    worker.tick()
    notices.clock.now += timedelta(seconds=1)
    raw = add_mention(notices)
    phone.sources[raw["message_id"]] = raw
    notices.service.tick()
    worker.tick()
    notices.service.tick()
    assert sent_phases(notices) == ["received", "waiting"]
    assert len(model.calls) == 1 and not phone.sent and not phone.calls


def test_old_attention_discovery_is_a_permanent_silent_baseline(notices):
    activate(notices)
    created = notices.clock.now + timedelta(minutes=1)
    notices.clock.now += timedelta(days=1)
    add_mention(notices, created=created)
    add_attention(notices, source_created_at=created, due_at=created + timedelta(minutes=15))
    notices.service.tick()
    for _ in range(2):
        notices.clock.now += timedelta(seconds=1)
        with notices.sessions.begin() as db:
            db.get(Message, "om_mention").synced_at = notices.clock.now
            db.get(MessageAttention, "om_mention").status = "normal"
        notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
        notices.service.tick()
    with notices.sessions() as db:
        source = db.scalar(select(StatusNoticeSource))
        assert source is not None and source.notify_changes is False
    assert not notices.feishu.sent and not notice_rows(notices)


@pytest.mark.parametrize("phase", ["received", "waiting"])
def test_expired_unsent_mention_is_not_replayed_when_permission_returns(notices, phase):
    activate(notices)
    add_mention(notices)
    if phase == "waiting":
        notices.service.tick()
        add_attention(notices)
    notices.feishu.ready = False
    notices.service.tick()
    before = len(notices.feishu.sent)
    pending = next(row for row in notice_rows(notices) if row.status == "blocked_permission")
    assert pending.phase == phase and pending.submitted_at is None
    notices.clock.now += timedelta(days=1)
    notices.feishu.ready = True
    for _ in range(3):
        notices.service.tick()
        notices.clock.now += timedelta(seconds=1)
        with notices.sessions.begin() as db:
            db.get(Message, "om_mention").synced_at = notices.clock.now
    assert len(notices.feishu.sent) == before
    expired = next(row for row in notice_rows(notices) if row.id == pending.id)
    assert expired.status == "superseded" and expired.error_code == "status_message_expired"
    assert expired.submitted_at is None


def test_real_submitted_phone_result_survives_an_overnight_delivery_wait(notices):
    activate(notices)
    add_mention(notices)
    add_attention(
        notices, status="phone_accepted", reminder_submitted_at=notices.clock.now,
        phone_submitted_at=notices.clock.now, phone_accepted_at=notices.clock.now,
    )
    notices.feishu.ready = False
    notices.service.tick()
    notices.clock.now += timedelta(days=1)
    notices.feishu.ready = True
    notices.service.tick()
    assert sent_phases(notices) == ["phone_accepted"]


@pytest.mark.parametrize("kind,expected", [("approval", 1), ("dms", 2)])
def test_approval_and_dms_notifications_keep_their_existing_delivery_policy(notices, kind, expected):
    activate(notices)
    if kind == "approval":
        add_pending(notices)
    else:
        add_dms_attention(notices, with_attention=False)
    notices.feishu.ready = False
    notices.service.tick()
    notices.clock.now += timedelta(days=1)
    notices.feishu.ready = True
    notices.service.tick()
    assert len(notices.feishu.sent) == expected
