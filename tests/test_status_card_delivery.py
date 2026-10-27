"""One durable status-card anchor per source, including ambiguous writes."""

from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select
from test_status_notifications import (
    activate,
    add_attention,
    add_dms_attention,
    add_human_review,
    add_mention,
    add_pending,
    complete_dms_card,
    notice_rows,
)
from test_status_notifications import notices as notices

from app.errors import AppError, FeishuError
from app.models import (
    Account,
    ApprovalReview,
    MessageAttention,
    ModelAssessment,
    StatusNoticeSource,
    StatusNotification,
)
from app.status_notifications import StatusNotificationService

pytestmark = pytest.mark.integration


def source(system):
    with system.sessions() as db:
        return db.scalar(select(StatusNoticeSource))


def restart(system):
    system.service = StatusNotificationService(system.settings, system.sessions, system.feishu)
    system.clock.now += timedelta(seconds=1)


def test_mention_changes_update_one_persisted_card_across_restarts(notices):
    activate(notices)
    add_mention(notices)
    notices.service.tick()
    add_attention(notices)
    restart(notices)
    notices.service.tick()
    with notices.sessions.begin() as db:
        db.get(MessageAttention, "om_mention").status = "responded"
    notices.service.tick()
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1
    assert len(notices.feishu.updated_cards) == 2
    anchor = source(notices)
    assert anchor.card_create_state == "ready" and anchor.card_message_id
    assert anchor.card_receipt["message_id"] == anchor.card_message_id
    assert all(message_id == anchor.card_message_id for message_id, _, _ in notices.feishu.updated_cards)
    assert {row.message_id for row in notice_rows(notices)} == {anchor.card_message_id}
    assert anchor.card_sent_revision == anchor.revision


def test_dms_receipt_and_conclusions_share_one_card(notices):
    activate(notices)
    identifier = add_dms_attention(notices)
    notices.service.tick()
    complete_dms_card(notices, identifier)
    notices.service.tick()
    restart(notices)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1
    assert len(notices.feishu.updated_cards) == 2
    assert {row.phase for row in notice_rows(notices)} == {"received", "needs_human", "responded"}


def test_unknown_create_blocks_all_later_creation_for_that_source(notices):
    activate(notices)
    add_pending(notices)
    notices.feishu.send_error = FeishuError("network")
    notices.service.tick()
    notices.feishu.send_error = None
    add_human_review(notices)
    restart(notices)
    for _ in range(3):
        notices.service.tick()
    assert len(notices.feishu.created_cards) == 1 and not notices.feishu.updated_cards
    assert source(notices).card_create_state == "unknown"
    assert {row.status for row in notice_rows(notices)} == {"unknown", "blocked_unknown"}


def test_returning_to_an_already_sent_state_patches_same_card_and_preserves_audit(notices):
    activate(notices)
    add_mention(notices)
    notices.service.tick()
    first = notice_rows(notices)[0]
    notices.clock.now += timedelta(seconds=1)
    add_attention(notices)
    notices.service.tick()
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        row = db.get(MessageAttention, "om_mention")
        row.status, row.model_result = "queued", {}
    notices.service.tick()
    rows = notice_rows(notices)
    assert len(notices.feishu.created_cards) == 1 and len(notices.feishu.updated_cards) == 2
    assert len(rows) == 3 and len({row.message_id for row in rows}) == 1
    copies = [row for row in rows if row.source_revision == first.source_revision]
    assert len(copies) == 2 and len({row.revision for row in copies}) == 2
    original = next(row for row in rows if row.id == first.id)
    assert original.submitted_at == first.submitted_at and original.sent_at == first.sent_at
    assert source(notices).card_sent_revision == first.source_revision


def test_unknown_patch_blocks_future_patch_and_never_creates_replacement(notices):
    activate(notices)
    add_mention(notices)
    notices.service.tick()
    add_attention(notices)
    notices.feishu.send_error = FeishuError("network")
    notices.service.tick()
    notices.feishu.send_error = None
    with notices.sessions.begin() as db:
        db.get(MessageAttention, "om_mention").status = "responded"
    restart(notices)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == len(notices.feishu.updated_cards) == 1
    assert any(row.status == "blocked_unknown" for row in notice_rows(notices))
    assert source(notices).card_create_state == "ready"


def test_successful_remote_create_with_failed_db_commit_is_not_recreated(notices, monkeypatch):
    activate(notices)
    add_pending(notices)
    original = notices.service._complete_delivery

    def fail_commit(*args, **kwargs):
        raise RuntimeError("result database unavailable")

    monkeypatch.setattr(notices.service, "_complete_delivery", fail_commit)
    with pytest.raises(RuntimeError):
        notices.service.tick()
    assert source(notices).card_create_state == "submitting"
    monkeypatch.setattr(notices.service, "_complete_delivery", original)
    restart(notices)
    notices.service.tick()
    add_human_review(notices)
    notices.service.tick()
    assert source(notices).card_create_state == "unknown"
    assert len(notices.feishu.created_cards) == 1 and not notices.feishu.updated_cards


def test_inflight_patch_recovery_is_unknown_without_another_write(notices, monkeypatch):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    add_human_review(notices)

    def fail_commit(*args, **kwargs):
        raise RuntimeError("result database unavailable")

    monkeypatch.setattr(notices.service, "_complete_delivery", fail_commit)
    with pytest.raises(RuntimeError):
        notices.service.tick()
    restart(notices)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == len(notices.feishu.updated_cards) == 1
    assert any(row.status == "unknown" and row.delivery_action == "update" for row in notice_rows(notices))


class PreflightError(AppError):
    write_attempted = False

    def __init__(self, retryable=True):
        super().__init__("status_card_unverified" if retryable else "status_card_expired", "test preflight")
        self.retryable = retryable


@pytest.mark.parametrize("stage", ["create", "update"])
def test_temporary_preflight_failure_remains_retryable_without_unknown_anchor(notices, stage):
    activate(notices)
    add_pending(notices)
    if stage == "update":
        notices.service.tick()
        add_human_review(notices)
    method = "send_self_status_card" if stage == "create" else "update_self_status_card"
    original = getattr(notices.feishu, method)
    setattr(notices.feishu, method, lambda *args, **kwargs: (_ for _ in ()).throw(PreflightError()))
    notices.service.tick()
    assert all(row.status != "unknown" for row in notice_rows(notices))
    assert source(notices).card_create_state == ("none" if stage == "create" else "ready")
    pending = next(row for row in notice_rows(notices) if row.status in {"queued", "blocked_permission"})
    assert pending.submitted_at is None
    setattr(notices.feishu, method, original)
    notices.clock.now += timedelta(seconds=61)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1
    assert len(notices.feishu.updated_cards) == (1 if stage == "update" else 0)


def test_expired_card_is_failed_without_a_replacement_card(notices):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    add_human_review(notices)
    notices.feishu.update_self_status_card = lambda *args, **kwargs: (_ for _ in ()).throw(PreflightError(False))
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1
    assert any(row.status == "failed" and row.error_code == "status_card_expired" for row in notice_rows(notices))
    with notices.sessions.begin() as db:
        review = db.get(ApprovalReview, "task_1")
        review.execution_status, review.confirmed_at = "approved", notices.clock.now
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1 and not notices.feishu.updated_cards


def test_update_marker_and_persisted_receipt_precede_remote_patch(notices):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    anchor = source(notices)
    add_human_review(notices)

    def check_marker(text, identifier):
        with notices.sessions() as db:
            row = db.get(StatusNotification, identifier)
            current = db.get(StatusNoticeSource, anchor.key)
            assert row.status == "sending" and row.delivery_action == "update"
            assert row.submitted_at and row.message_id == anchor.card_message_id
            assert row.card_payload and row.text == text
            assert current.card_create_state == "ready" and current.card_receipt == anchor.card_receipt
        concurrent = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
        assert concurrent.tick() is None

    notices.feishu.before_send = check_marker
    notices.service.tick()
    assert len(notices.feishu.created_cards) == len(notices.feishu.updated_cards) == 1


def test_owner_change_never_patches_a_previously_created_card(notices):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    add_human_review(notices)
    with notices.sessions.begin() as db:
        db.get(Account, 1).open_id = "ou_other"
    with pytest.raises(AppError):
        notices.service.tick()
    assert len(notices.feishu.created_cards) == 1 and not notices.feishu.updated_cards


@pytest.mark.parametrize("bad_result", [
    {"message_id": "om_no_receipt"},
    {"message_id": "om_other", "receipt": {"message_id": "om_mismatch"}},
])
def test_incomplete_create_receipt_never_becomes_an_update_anchor(notices, bad_result):
    activate(notices)
    add_pending(notices)
    attempts = []
    notices.feishu.send_self_status_card = lambda *args, **kwargs: attempts.append(True) or bad_result
    notices.service.tick()
    add_human_review(notices)
    restart(notices)
    notices.service.tick()
    assert attempts == [True]
    assert source(notices).card_create_state == "unknown"
    assert source(notices).card_message_id is None and not notices.feishu.updated_cards


def test_migration_preserves_text_history_without_replaying_existing_source(notices):
    activate(notices)
    notices.feishu.ready = False
    add_pending(notices)
    notices.service.tick()
    original = notice_rows(notices)[0]
    with notices.sessions.begin() as db:
        anchor = db.get(StatusNoticeSource, original.source_key)
        old_text = anchor.payload["text"]
        row = db.get(StatusNotification, original.id)
        row.text, row.status, row.message_id = old_text, "sent", "om_legacy_text"
        row.submitted_at = row.sent_at = notices.clock.now
        queued_id = str(uuid4())
        db.add(StatusNotification(
            id=queued_id, source_key=anchor.key, owner_open_id="ou_owner",
            revision="f" * 64, phase="received", text="old unsent text", status="queued",
        ))
    config = Config("alembic.ini")
    command.downgrade(config, "0009")
    command.upgrade(config, "head")
    notices.feishu.ready = True
    restart(notices)
    notices.service.tick()
    with notices.sessions() as db:
        sent, queued = db.get(StatusNotification, original.id), db.get(StatusNotification, queued_id)
        assert sent.delivery_action == queued.delivery_action == "legacy"
        assert sent.text == old_text and sent.status == "sent" and sent.message_id == "om_legacy_text"
        assert queued.status == "superseded" and queued.error_code == "status_legacy_migrated"
        assert db.get(StatusNoticeSource, original.source_key).card_message_id is None
    assert not notices.feishu.created_cards and not notices.feishu.updated_cards
    add_human_review(notices)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1 and not notices.feishu.updated_cards
    assert source(notices).card_message_id != "om_legacy_text"
    # A was legacy text, B created the first card, and a real return to A must
    # update that card without altering or reusing the old text delivery.
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        db.execute(delete(ApprovalReview).where(ApprovalReview.task_id == "task_1"))
        db.execute(delete(ModelAssessment).where(ModelAssessment.task_id == "task_1"))
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1 and len(notices.feishu.updated_cards) == 1
    with notices.sessions() as db:
        legacy = db.get(StatusNotification, original.id)
        assert legacy.delivery_action == "legacy" and legacy.message_id == "om_legacy_text"
        assert legacy.submitted_at == notices.clock.now - timedelta(seconds=2)
    restart(notices)
    notices.service.tick()
    assert len(notices.feishu.created_cards) == 1
