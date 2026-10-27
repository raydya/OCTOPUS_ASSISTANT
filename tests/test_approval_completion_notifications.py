"""A task completed in Feishu must finish its existing owner-status card."""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_status_notifications import activate, add_human_review, add_pending, notice_rows
from test_status_notifications import notices as notices

from app.models import (
    ApprovalInstance,
    ApprovalReview,
    ApprovalTask,
    StatusNoticeSource,
    StatusNotification,
)

pytestmark = pytest.mark.integration


def _completed_outside_app(system, *, change=None):
    """Simulate targeted instance refresh after the user approves in Feishu."""
    system.clock.now += timedelta(seconds=1)
    with system.sessions.begin() as db:
        task = db.get(ApprovalTask, ("task_1", "1"))
        task.visible = False
        task.synced_at = system.clock.now
        review = db.get(ApprovalReview, "task_1")
        review.decision = review.execution_status = "not_pending"
        review.reviewed_at = system.clock.now
        instance = db.get(ApprovalInstance, task.instance_code)
        detail = deepcopy(instance.raw)
        detail["tasks"][0]["status"] = "APPROVED"
        # The user's node is complete while the overall approval can continue.
        detail["status"] = "PENDING"
        detail["tasks"].append({
            "id": "task_next", "user_id": "ou_next", "status": "PENDING", "node_id": "node_next",
        })
        if change:
            change(detail)
        instance.raw, instance.synced_at = detail, system.clock.now


def _project(system):
    with system.sessions() as db:
        task = db.get(ApprovalTask, ("task_1", "1"))
        return system.service._approval(db, task, "ou_owner")


def _pending_card(system):
    activate(system)
    add_pending(system)
    add_human_review(system)
    system.service.tick()
    assert len(system.feishu.created_cards) == 1


def test_cached_own_approved_task_is_shown_as_completed_without_app_submission(notices):
    _pending_card(notices)
    _completed_outside_app(notices)

    payload = _project(notices)

    assert payload["phase"] == "approved"
    assert payload["card"]["status"] == "✅ 已通过"
    with notices.sessions() as db:
        review = db.get(ApprovalReview, "task_1")
        assert review.execution_status == "not_pending"
        assert review.submitted_at is None and review.confirmed_at is None


def test_manual_approval_updates_existing_card_once_after_pending_disappears(notices):
    _pending_card(notices)
    first_notice = notice_rows(notices)[0]
    _completed_outside_app(notices)

    notices.service.tick()
    notices.service.tick()

    assert len(notices.feishu.created_cards) == 1
    message_id, card, _ = notices.feishu.updated_cards[-1]
    assert message_id == first_notice.message_id
    assert card["elements"][0]["text"]["content"] == "✅ 已通过"
    assert len(notices.feishu.updated_cards) == 1
    with notices.sessions() as db:
        source = db.scalar(select(StatusNoticeSource))
        assert source.payload["phase"] == "approved"
        assert source.card_message_id == first_notice.message_id
    assert [row.phase for row in notice_rows(notices)] == ["needs_human", "approved"]


@pytest.mark.parametrize("change", [
    lambda detail: detail.update(instance_code="instance_other"),
    lambda detail: detail["tasks"][0].update(id="task_other"),
    lambda detail: detail["tasks"][0].update(user_id="ou_other"),
    lambda detail: detail["tasks"][0].update(status="REJECTED"),
    lambda detail: detail["tasks"].append(deepcopy(detail["tasks"][0])),
    lambda detail: detail.update(status="APPROVED", tasks=[]),
])
def test_completion_requires_matching_unique_approved_owner_task(notices, change):
    _pending_card(notices)
    _completed_outside_app(notices, change=change)

    payload = _project(notices)

    assert payload["phase"] != "approved"
    assert payload["card"]["status"] != "✅ 已通过"


@pytest.mark.parametrize("status,phase,label", [
    ("APPROVED", "approved", "✅ 已通过"),
    ("REJECTED", "rejected", "❌ 已拒绝"),
    ("TRANSFERRED", "transferred", "↪️ 已转交"),
    ("DONE", "completed", "ℹ️ 本人节点已结束"),
])
def test_each_verified_owner_task_outcome_has_its_own_completion_label(notices, status, phase, label):
    _pending_card(notices)
    _completed_outside_app(notices, change=lambda detail: detail["tasks"][0].update(status=status))

    payload = _project(notices)

    assert payload["phase"] == phase
    assert payload["terminal"] is True
    assert payload["card"]["status"] == label


@pytest.mark.parametrize("change", [
    lambda detail: detail.update(tasks=None),
    lambda detail: detail.update(tasks={"id": "task_1", "user_id": "ou_owner", "status": "APPROVED"}),
    lambda detail: detail["tasks"][0].update(status=["APPROVED"]),
    lambda detail: detail["tasks"][0].pop("user_id"),
    lambda detail: detail["tasks"][0].update(status="PENDING"),
    lambda detail: detail["tasks"][0].update(status="UNKNOWN"),
])
def test_unverifiable_task_result_retains_pending_disappearance_status(notices, change):
    _pending_card(notices)
    _completed_outside_app(notices, change=change)

    payload = _project(notices)

    assert payload["phase"] == "responded"
    assert payload["card"]["status"] == "ℹ️ 已不在当前待办"


def test_old_approved_detail_cannot_finish_a_newer_pending_task_observation(notices):
    _pending_card(notices)
    _completed_outside_app(notices)
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        task = db.get(ApprovalTask, ("task_1", "1"))
        task.visible = True
        task.synced_at = notices.clock.now

    payload = _project(notices)

    assert payload["phase"] != "approved"
    assert payload["card"]["status"] != "✅ 已通过"


def test_current_rejected_task_overrides_old_confirmed_approval_display(notices):
    _pending_card(notices)
    with notices.sessions.begin() as db:
        review = db.get(ApprovalReview, "task_1")
        review.execution_status = "approved"
        review.submitted_at = review.confirmed_at = notices.clock.now
    _completed_outside_app(notices, change=lambda detail: detail["tasks"][0].update(status="REJECTED"))
    with notices.sessions.begin() as db:
        # Preserve the prior execution audit while a newer instance read gives
        # the current task result, as can happen after a workflow rollback.
        db.get(ApprovalReview, "task_1").execution_status = "approved"

    payload = _project(notices)

    assert payload["phase"] == "rejected"
    assert payload["card"]["status"] == "❌ 已拒绝"
    with notices.sessions() as db:
        assert db.get(ApprovalReview, "task_1").execution_status == "approved"


@pytest.mark.parametrize("has_card", [False, True])
def test_existing_completion_evidence_updates_cards_but_does_not_replay_legacy_history(notices, has_card):
    _pending_card(notices)
    _completed_outside_app(notices, change=lambda detail: detail["tasks"][0].update(status="PENDING"))
    notices.service.tick()
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        task = db.get(ApprovalTask, ("task_1", "1"))
        instance = db.get(ApprovalInstance, task.instance_code)
        detail = deepcopy(instance.raw)
        detail["tasks"][0]["status"] = "APPROVED"
        instance.raw = detail
        source = db.scalar(select(StatusNoticeSource))
        source.updated_at = notices.clock.now
        assert task.synced_at <= instance.synced_at < source.updated_at
        if not has_card:
            source.card_message_id = source.card_receipt = source.card_sent_revision = None
            source.card_create_state = "none"
            for row in db.scalars(select(StatusNotification)):
                row.delivery_action = "legacy"
    notices.feishu.created_cards.clear()
    notices.feishu.updated_cards.clear()

    notices.service.tick()
    notices.service.tick()

    assert not notices.feishu.created_cards
    assert len(notices.feishu.updated_cards) == int(has_card)
    if has_card:
        assert notices.feishu.updated_cards[0][1]["elements"][0]["text"]["content"] == "✅ 已通过"
    else:
        # A genuinely newer targeted read is new evidence. Once observed, its
        # queued payload must survive _deliver's second projection even though
        # _observe has advanced source.updated_at to the current time.
        notices.clock.now += timedelta(seconds=1)
        with notices.sessions.begin() as db:
            db.get(ApprovalInstance, "instance_task_1").synced_at = notices.clock.now
        notices.service.tick()
        assert len(notices.feishu.created_cards) == 1
        card = notices.feishu.created_cards[0][0]
        assert card["elements"][0]["text"]["content"] == "✅ 已通过"


@pytest.mark.parametrize("has_card", [False, True])
def test_days_old_completion_cache_newer_than_legacy_source_does_not_create_a_card(notices, has_card):
    _pending_card(notices)
    _completed_outside_app(notices, change=lambda detail: detail["tasks"][0].update(status="PENDING"))
    notices.service.tick()
    with notices.sessions.begin() as db:
        source = db.scalar(select(StatusNoticeSource))
        instance = db.get(ApprovalInstance, "instance_task_1")
        detail = deepcopy(instance.raw)
        detail["tasks"][0]["status"] = "APPROVED"
        instance.raw = detail
        instance.synced_at = source.updated_at + timedelta(seconds=1)
        if not has_card:
            source.card_message_id = source.card_receipt = source.card_sent_revision = None
            source.card_create_state = "none"
            for row in db.scalars(select(StatusNotification)):
                row.delivery_action = "legacy"
    notices.clock.now += timedelta(days=4)
    notices.feishu.created_cards.clear()
    notices.feishu.updated_cards.clear()

    notices.service.tick()
    notices.service.tick()

    assert not notices.feishu.created_cards
    assert len(notices.feishu.updated_cards) == int(has_card)
    if has_card:
        assert notices.feishu.updated_cards[0][1]["elements"][0]["text"]["content"] == "✅ 已通过"
