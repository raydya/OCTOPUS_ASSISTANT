import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_approval_review import FakeFeishu as ApprovalFeishu
from test_approval_review import review_setup as review_setup
from test_attention import FakeFeishu as MessageFeishu
from test_attention import FakeModel as MessageModel
from test_attention import add_message
from test_model_review import model_result
from test_model_review import setup_model as setup_model

from app import approval_review, attention, model_review
from app.attention import AttentionService
from app.errors import AppError, FeishuError
from app.models import (
    Account,
    ApprovalReview,
    ApprovalTask,
    AttentionState,
    Chat,
    MessageAttention,
    ModelAssessment,
    SyncState,
    utcnow,
)

pytestmark = pytest.mark.integration


def approval_attention_id(task_id="task_1"):
    return "approval_" + hashlib.sha256(task_id.encode()).hexdigest()


class CombinedFeishu(ApprovalFeishu, MessageFeishu):
    def __init__(self):
        ApprovalFeishu.__init__(self)
        MessageFeishu.__init__(self)
        self.instance_callback = None
        self.instance_reads = 0

    def instance(self, instance_code):
        self.instance_reads += 1
        if self.instance_callback:
            self.instance_callback(self.instance_reads)
        return ApprovalFeishu.instance(self, instance_code)

    def send_self_reminder(self, text, uuid):
        MessageFeishu.send_self_reminder(self, text, uuid)
        return {"message_id": f"om_reminder_{len(self.sent)}"}


@pytest.fixture
def approval_system(setup_model, monkeypatch):
    reviewer, model_service, sessions, _, provider = setup_model
    clock = SimpleNamespace(now=utcnow())
    for module in (approval_review, model_review, attention):
        monkeypatch.setattr(module, "utcnow", lambda: clock.now)
    settings = reviewer.settings
    settings.attention_enabled = True
    settings.attention_timeout_seconds = 900
    settings.attention_call_cooldown_seconds = 1800
    settings.approval_model_timeout_seconds = 180
    settings.feishu_app_id = "cli_test"
    feishu, message_model = CombinedFeishu(), MessageModel()
    reviewer.feishu = feishu
    provider.result = model_result(
        decision="needs_human", risk="medium",
        summary="需要本人补充验收结论。",
        questions=["请提供该申请的验收结论。"],
        missing_evidence=["验收结论"],
    )
    with sessions.begin() as db:
        db.add(Chat(
            chat_id="oc_team", name="业务群", chat_mode="group", raw={},
            generation="test", visible=True, synced_at=clock.now,
        ))
    service = AttentionService(settings, sessions, feishu, message_model)
    service.tick()  # Initialize the shared durable discovery/cooldown state.
    return SimpleNamespace(
        reviewer=reviewer, model_service=model_service, sessions=sessions,
        feishu=feishu, provider=provider, model=message_model,
        service=service, clock=clock, settings=settings,
    )


def attention_record(system, identifier=None):
    with system.sessions() as db:
        return db.get(MessageAttention, identifier or approval_attention_id())


def discover_human_approval(system):
    review = system.reviewer.tick()
    assert review["execution_status"] == "awaiting_review"
    assert review["model_review"]["decision"] == "needs_human"
    system.clock.now += timedelta(seconds=1)
    system.service.tick()
    record = attention_record(system)
    assert record is not None
    return record


def fresh_local_pending(system):
    with system.sessions.begin() as db:
        state = db.get(SyncState, "approvals:1")
        state.completed_at = state.synced_at = system.clock.now
        task = db.get(ApprovalTask, ("task_1", "1"))
        task.synced_at = system.clock.now
        if system.feishu.pending:
            task.raw = deepcopy(system.feishu.pending[0])
        review = db.get(ApprovalReview, "task_1")
        if review:
            review.reviewed_at = system.clock.now - timedelta(seconds=61)


def give_feedback(system, action):
    with system.sessions() as db:
        review = db.get(ApprovalReview, "task_1")
        assessment_id, snapshot = review.assessment_id, review.snapshot_hash
    return system.model_service.respond(
        "task_1", assessment_id, snapshot, action,
        text="本人已开始处理并补充核验信息。" if action == "answer" else "",
        current_snapshot=snapshot,
    )


def test_human_approval_starts_one_fifteen_minute_timer_and_uses_existing_model_judgment(
    approval_system,
):
    system = approval_system
    record = discover_human_approval(system)
    discovered_at, due_at = system.clock.now, record.due_at
    assert record.source_type == "approval"
    assert record.approval_task_id == "task_1"
    assert record.approval_instance_code == "instance_task_1"
    assert record.approval_assessment_id
    assert due_at == discovered_at + timedelta(minutes=15)
    assert record.status == "waiting"
    for delay in (30, 60, 300):
        system.clock.now += timedelta(seconds=delay)
        system.service.tick()
        assert attention_record(system).due_at == due_at
    system.clock.now = due_at - timedelta(microseconds=1)
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls

    def assert_committed_phone_marker():
        current = attention_record(system)
        assert current.phone_submitted_at is not None
        with system.sessions() as db:
            assert db.get(AttentionState, 1).last_call_submitted_at == current.phone_submitted_at

    system.feishu.phone_callback = assert_committed_phone_marker
    system.clock.now = due_at
    result = system.service.tick()
    assert result["status"] == "phone_accepted"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert len(system.provider.calls) == 1
    assert not system.model.calls and not system.feishu.posts
    assert not system.feishu.message_reads and not system.feishu.list_calls
    assert "验收" in system.feishu.sent[0][0]
    assert "审批" in system.feishu.sent[0][0]
    assert "接听" in result["reasons"][0] and "无法" in result["reasons"][0]
    system.clock.now += timedelta(hours=2)
    for _ in range(3):
        system.service.tick()
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1


@pytest.mark.parametrize("action", ["answer", "approve", "hold"])
def test_local_human_feedback_stops_overdue_approval_phone(approval_system, action):
    system = approval_system
    record = discover_human_approval(system)
    give_feedback(system, action)
    system.clock.now = record.due_at + timedelta(seconds=1)
    system.service.tick()
    assert attention_record(system).status == "responded"
    assert not system.feishu.sent and not system.feishu.calls
    assert not system.feishu.posts


@pytest.mark.parametrize("change", ["approved", "reassigned", "missing", "node_removed", "reverted"])
def test_fresh_remote_approval_status_blocks_obsolete_calls(approval_system, change):
    system = approval_system
    record = discover_human_approval(system)
    detail = system.feishu.details["instance_task_1"]
    if change == "approved":
        detail["tasks"][0]["status"] = "APPROVED"
    elif change == "reassigned":
        system.feishu.pending[0]["user_id"] = "ou_other"
        detail["tasks"][0]["user_id"] = "ou_other"
    elif change == "missing":
        system.feishu.pending = []
    elif change == "node_removed":
        detail["current_nodes"] = []
    else:
        detail["reverted"] = True
    system.clock.now = record.due_at
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls
    assert attention_record(system).phone_submitted_at is None


def test_incomplete_remote_pending_list_never_means_safe_to_phone(approval_system):
    system = approval_system
    record = discover_human_approval(system)
    system.feishu.pending = []
    system.feishu.has_more = True  # Deliberately no valid continuation token.
    system.clock.now = record.due_at
    system.service.tick()
    assert attention_record(system).status == "blocked"
    assert not system.feishu.sent and not system.feishu.calls


def test_new_approval_snapshot_gets_new_timer_only_after_model_review_updates(approval_system):
    system = approval_system
    original = discover_human_approval(system)
    original_due, old_assessment = original.due_at, original.approval_assessment_id
    detail = system.feishu.details["instance_task_1"]
    form = json.loads(detail["form"])
    form[0]["value"] = "补充了新的测试资源用途，请重新核验。"
    detail["form"] = json.dumps(form)
    system.clock.now = original_due
    system.service.tick()
    assert attention_record(system).status == "source_changed"
    assert not system.feishu.sent
    fresh_local_pending(system)
    assert system.reviewer.tick()["execution_status"] == "awaiting_review"
    system.clock.now += timedelta(seconds=1)
    system.service.tick()
    current = attention_record(system)
    assert current.status == "waiting"
    assert current.due_at == system.clock.now + timedelta(minutes=15)
    assert current.due_at > original_due
    assert current.approval_assessment_id != old_assessment
    assert current.source_hash != original.source_hash
    assert len(system.provider.calls) == 2
    assert not system.model.calls and not system.feishu.calls


def test_missing_phone_permission_blocks_but_rechecks_when_granted(approval_system):
    system = approval_system
    record = discover_human_approval(system)
    system.clock.now = record.due_at
    system.feishu.ready = False
    assert system.service.tick()["status"] == "blocked_permission"
    assert attention_record(system).reminder_submitted_at is None
    assert not system.feishu.sent and not system.feishu.calls
    system.feishu.ready = True
    system.clock.now += timedelta(seconds=61)
    assert system.service.tick()["status"] == "phone_accepted"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1


@pytest.mark.parametrize("first_source", ["approval", "message"])
def test_approval_and_mention_share_one_owner_wide_call_cooldown(approval_system, first_source):
    system = approval_system
    if first_source == "approval":
        discover_human_approval(system)
        add_message(system)
        system.service.tick()
    else:
        add_message(system)
        system.service.tick()
        discover_human_approval(system)
    identifiers = {
        "approval": approval_attention_id(),
        "message": "om_source",
    }
    second_source = "message" if first_source == "approval" else "approval"
    first = attention_record(system, identifiers[first_source])
    second = attention_record(system, identifiers[second_source])
    system.clock.now = first.due_at
    assert system.service.tick()["message_id"] == identifiers[first_source]
    assert len(system.feishu.calls) == 1
    system.clock.now = max(system.clock.now, second.due_at) + timedelta(seconds=1)
    result = system.service.tick()
    assert result["message_id"] == identifiers[second_source]
    assert result["status"] == "waiting"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    waiting = attention_record(system, identifiers[second_source])
    assert waiting.next_attempt_at >= first.due_at + timedelta(seconds=1800)
    system.clock.now = waiting.next_attempt_at
    assert system.service.tick()["status"] == "phone_accepted"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 2


@pytest.mark.parametrize("stage", ["before_send", "after_send"])
@pytest.mark.parametrize("response", ["ack", "feedback"])
def test_response_during_approval_network_work_stops_phone(approval_system, stage, response):
    system = approval_system
    record = discover_human_approval(system)
    system.clock.now = record.due_at

    def respond():
        if response == "ack":
            system.service.acknowledge(approval_attention_id())
        else:
            give_feedback(system, "answer")

    if stage == "before_send":
        def after_instance_read(_):
            system.feishu.instance_callback = None
            respond()

        system.feishu.instance_callback = after_instance_read
    else:
        system.feishu.send_callback = respond
    result = system.service.tick()
    assert result["status"] in {"responded", "acknowledged"}
    assert len(system.feishu.sent) == (1 if stage == "after_send" else 0)
    assert not system.feishu.calls
    assert attention_record(system).phone_submitted_at is None


@pytest.mark.parametrize("stage", ["send", "phone"])
def test_unknown_approval_notification_result_is_never_replayed_after_restart(
    approval_system, stage,
):
    system = approval_system
    record = discover_human_approval(system)
    system.clock.now = record.due_at
    if stage == "send":
        system.feishu.send_error = FeishuError("network")
    else:
        system.feishu.phone_error = FeishuError("network")
    result = system.service.tick()
    assert result["status"] == ("reminder_unknown" if stage == "send" else "phone_unknown")
    system.clock.now += timedelta(hours=1)
    restarted = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    for _ in range(3):
        restarted.tick()
    assert len(system.feishu.sent) == 1
    assert len(system.feishu.calls) == (0 if stage == "send" else 1)


@pytest.mark.parametrize("error", [
    AppError("approval_model_busy", "模型忙。", 503),
    TimeoutError("synthetic timeout"),
])
def test_failed_or_busy_model_is_not_misclassified_as_business_needs_human(
    approval_system, error,
):
    system = approval_system
    system.provider.error = error
    review = system.reviewer.tick()
    assert review["execution_status"] == "awaiting_review"
    assert review["model_review"]["status"] != "complete"
    system.service.tick()
    system.clock.now += timedelta(minutes=16)
    system.service.tick()
    assert attention_record(system) is None
    assert not system.feishu.sent and not system.feishu.calls


def test_stale_complete_view_with_in_progress_assessment_cannot_trigger_phone(approval_system):
    system = approval_system
    review = system.reviewer.tick()
    with system.sessions.begin() as db:
        db.get(ModelAssessment, review["assessment_id"]).status = "assessing"
    system.service.tick()
    system.clock.now += timedelta(minutes=16)
    system.service.tick()
    assert attention_record(system) is None
    assert not system.feishu.sent and not system.feishu.calls


def test_model_approved_but_write_permission_blocked_is_not_human_business_review(approval_system):
    system = approval_system
    system.provider.result = model_result()
    with system.sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:read"
    assert system.reviewer.tick()["execution_status"] == "blocked_permission"
    system.service.tick()
    system.clock.now += timedelta(minutes=16)
    system.service.tick()
    assert attention_record(system) is None
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.parametrize("already", ["acknowledged", "called"])
def test_new_assessment_does_not_repeat_an_acknowledged_or_called_task(approval_system, already):
    system = approval_system
    record = discover_human_approval(system)
    if already == "acknowledged":
        system.service.acknowledge(record.message_id)
    else:
        system.clock.now = record.due_at
        assert system.service.tick()["status"] == "phone_accepted"
    counts = len(system.feishu.sent), len(system.feishu.calls)
    detail = system.feishu.details["instance_task_1"]
    form = json.loads(detail["form"])
    form[0]["value"] = "同一单据后续修改，不能再次呼叫。"
    detail["form"] = json.dumps(form)
    system.clock.now += timedelta(minutes=1)
    fresh_local_pending(system)
    system.reviewer.tick()
    system.service.tick()
    system.clock.now += timedelta(hours=1)
    system.service.tick()
    assert (len(system.feishu.sent), len(system.feishu.calls)) == counts
    with system.sessions() as db:
        assert db.scalar(select(func.count()).select_from(MessageAttention)) == 1


def test_overdue_unsent_approval_resumes_after_restart_with_fresh_verification(approval_system):
    system = approval_system
    record = discover_human_approval(system)
    original_due = record.due_at
    system.clock.now = original_due + timedelta(minutes=2)
    restarted = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    reads_before = system.feishu.reads
    result = restarted.tick()
    assert result["status"] == "phone_accepted"
    assert attention_record(system).due_at == original_due
    assert system.feishu.reads > reads_before
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert len(system.provider.calls) == 1 and not system.model.calls
    assert restarted.tick() is None
    assert len(system.feishu.calls) == 1


def test_approval_cooldown_survives_restart_and_rechecks_when_original_deadline_arrives(
    approval_system,
):
    system = approval_system
    add_message(system)
    system.service.tick()
    approval = discover_human_approval(system)
    mention = attention_record(system, "om_source")
    system.clock.now = mention.due_at
    assert system.service.tick()["status"] == "phone_accepted"
    system.clock.now = approval.due_at
    assert system.service.tick()["status"] == "waiting"
    deadline = attention_record(system).next_attempt_at
    assert deadline == mention.due_at + timedelta(seconds=1800)

    system.clock.now += timedelta(seconds=30)
    restarted = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    reads_before = system.feishu.reads
    assert restarted.tick() is None
    current = attention_record(system)
    assert current.status == "waiting"
    assert current.due_at == approval.due_at
    assert current.next_attempt_at == deadline
    system.clock.now = deadline - timedelta(microseconds=1)
    assert restarted.tick() is None
    assert system.feishu.reads == reads_before
    assert len(system.feishu.calls) == 1
    system.clock.now = deadline
    assert restarted.tick()["status"] == "phone_accepted"
    assert system.feishu.reads > reads_before
    assert len(system.feishu.sent) == len(system.feishu.calls) == 2
