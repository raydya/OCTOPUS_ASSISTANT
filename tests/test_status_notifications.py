"""Owner-only status notices use fake delivery and never alter call/approval state."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_approval_review import sample_detail, sample_task
from test_attention import IMPORTANT, decision, raw_message
from test_dms_attention import card as dms_card

from app import status_notifications
from app.attention import source_hash
from app.dms_attention import _metadata, _parse, _presentation, dms_attention_id
from app.errors import AppError, FeishuError
from app.model_review import business_snapshot
from app.models import (
    Account,
    ApprovalInstance,
    ApprovalReview,
    ApprovalTask,
    AttentionState,
    Chat,
    Message,
    MessageAttention,
    ModelAssessment,
    StatusNoticeSource,
    StatusNotification,
    utcnow,
)
from app.status_notice_content import dms_receipt_revision
from app.status_notifications import StatusNotificationService

pytestmark = pytest.mark.integration


class FakeStatusFeishu:
    def __init__(self):
        self.ready = True
        self.sent = []
        self.before_send = None
        self.send_error = None
        self.created_cards = []
        self.updated_cards = []
        self.notice_for_send = None
        self.now = utcnow

    def self_message_permissions(self):
        return {
            "ready": self.ready,
            "missing_scopes": [] if self.ready else ["im:message:send_as_bot"],
        }

    def send_self_status(self, text, uuid, *, expected_owner):
        assert expected_owner == "ou_owner"
        if self.before_send:
            self.before_send(text, uuid)
        self.sent.append((text, uuid, expected_owner))
        if self.send_error:
            raise self.send_error
        return {"message_id": f"om_status_{len(self.sent)}"}

    def send_self_status_card(self, card, uuid, *, expected_owner):
        self.created_cards.append((deepcopy(card), uuid, expected_owner))
        notice = self.notice_for_send(uuid) if self.notice_for_send else None
        result = self.send_self_status(notice.text if notice else str(card), uuid, expected_owner=expected_owner)
        return {**result, "receipt": {
            "message_id": result["message_id"], "chat_id": "oc_owner_dm",
            "owner_open_id": expected_owner, "sender_app_id": "cli_test",
            "msg_type": "interactive", "create_time": str(int(self.now().timestamp() * 1000)),
        }}

    def update_self_status_card(self, message_id, card, *, expected_owner, expected_receipt):
        assert expected_receipt["message_id"] == message_id
        assert expected_receipt["owner_open_id"] == expected_owner
        self.updated_cards.append((message_id, deepcopy(card), deepcopy(expected_receipt)))
        notice = self.notice_for_send(None)
        self.send_self_status(notice.text, notice.id, expected_owner=expected_owner)
        return {"message_id": message_id}

    def send_self_reminder(self, *args, **kwargs):
        pytest.fail("Status delivery must not submit an overdue reminder")

    def phone_self(self, *args, **kwargs):
        pytest.fail("Status delivery must not place a telephone call")

    def approve_task(self, *args, **kwargs):
        pytest.fail("Status delivery must not approve an application")


@pytest.fixture
def notices(database, monkeypatch):
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    monkeypatch.setattr(status_notifications, "utcnow", lambda: clock.now)
    settings = SimpleNamespace(
        status_notifications_enabled=True,
        feishu_app_id="cli_test",
        app_base_url="http://localhost:8000",
        attention_timeout_seconds=900,
        attention_call_cooldown_seconds=1800,
        dms_sender_app_id="cli_dms",
        dms_chat_id="oc_dms",
    )
    sessions = sessionmaker(database, expire_on_commit=False)
    feishu = FakeStatusFeishu()
    feishu.now = lambda: clock.now

    def notice_for_send(identifier):
        with sessions() as db:
            if identifier:
                return db.get(StatusNotification, identifier)
            return db.scalar(select(StatusNotification).where(StatusNotification.status == "sending"))

    feishu.notice_for_send = notice_for_send
    with sessions.begin() as db:
        db.add(Account(
            id=1, open_id="ou_owner", tenant_key="tenant", name="本人",
            access_token="unused", scope="", expires_at=clock.now + timedelta(hours=2),
        ))
        db.add(Chat(
            chat_id="oc_team", name="业务群", chat_mode="group", raw={},
            generation="test", visible=True, synced_at=clock.now,
        ))
    return SimpleNamespace(
        service=StatusNotificationService(settings, sessions, feishu),
        sessions=sessions, feishu=feishu, clock=clock, settings=settings,
    )


def activate(system):
    system.service.tick()
    assert not system.feishu.sent
    system.clock.now += timedelta(seconds=1)


def add_mention(system, message_id="om_mention", *, created=None, changes=None):
    raw = raw_message(created or system.clock.now, message_id, **(changes or {}))
    with system.sessions.begin() as db:
        db.add(Message(
            message_id=message_id, chat_id=raw["chat_id"], msg_type=raw["msg_type"],
            sender_name="发起人", text=IMPORTANT, create_time=int(raw["create_time"]),
            deleted=bool(raw.get("deleted")), raw=raw, synced_at=system.clock.now,
        ))
    return raw


def add_pending(system, task_id="task_1", *, owner="ou_owner"):
    raw = {**sample_task(task_id), "user_id": owner}
    with system.sessions.begin() as db:
        db.add(ApprovalTask(
            task_id=task_id, topic="1", instance_code=raw["instance_code"],
            title=f"测试审批 {task_id}", status="1", initiator_name="申请人",
            raw=raw, visible=True, generation="test", synced_at=system.clock.now,
        ))
    return raw


def add_attention(system, message_id="om_mention", *, important=True, **changes):
    with system.sessions.begin() as db:
        message = db.get(Message, message_id)
        values = {
            "message_id": message_id,
            "owner_open_id": "ou_owner",
            "source_type": "message",
            "source_data": {},
            "chat_id": message.chat_id,
            "chat_name": "业务群",
            "text": IMPORTANT,
            "source_created_at": system.clock.now,
            "source_hash": source_hash(message.raw),
            "due_at": system.clock.now + timedelta(minutes=15),
            "status": "waiting" if important else "normal",
            "model_result": decision(
                importance="high" if important else "normal",
                requires_response=important,
                summary="需要立即处理服务故障。" if important else "常规同步，无需立即回复。",
            ),
            "reasons": ["已完成重要性判断。"],
            "assessed_at": system.clock.now,
            "created_at": system.clock.now,
            "updated_at": system.clock.now,
            **changes,
        }
        db.add(MessageAttention(**values))


def add_human_review(system, task_id="task_1"):
    assessment_id = str(uuid4())
    detail = sample_detail(task_id)
    with system.sessions.begin() as db:
        task = db.get(ApprovalTask, (task_id, "1"))
        snapshot = business_snapshot(task.raw, detail, "ou_owner")
        judgment = {
            "status": "complete", "source": "model", "decision": "needs_human",
            "eligible": False, "risk": "medium", "summary": "需要本人核实验收结论。",
            "reasons": ["申请缺少验收结论。"], "questions": ["验收是否已经完成？"],
            "missing_evidence": ["验收结论"], "evidence": [],
            "assessment_id": assessment_id, "task_id": task_id,
            "snapshot_hash": snapshot, "model": "gpt-6-sol", "reasoning_effort": "xhigh",
        }
        db.add(ApprovalInstance(
            instance_code=task.instance_code, raw=detail, synced_at=system.clock.now,
        ))
        db.add(ModelAssessment(
            id=assessment_id, cache_key=assessment_id, task_id=task_id,
            instance_code=task.instance_code, owner_open_id="ou_owner", snapshot_hash=snapshot,
            model="gpt-6-sol", reasoning_effort="xhigh", prompt_version="approval-v1",
            status="complete", result=deepcopy(judgment), usage={},
            created_at=system.clock.now, updated_at=system.clock.now,
        ))
        db.add(ApprovalReview(
            task_id=task_id, instance_code=task.instance_code, owner_open_id="ou_owner",
            decision="needs_review", execution_status="awaiting_review",
            policy_version="test", fingerprint=snapshot, reasons=judgment["reasons"],
            assessment_id=assessment_id, snapshot_hash=snapshot, model_review=judgment,
            reviewed_at=system.clock.now, created_at=system.clock.now,
        ))


def dms_attention_row(raw, created):
    parsed = _parse(raw)
    identifier = dms_attention_id("cli_dms", "oc_dms", parsed["task_key"], "ou_owner")
    return MessageAttention(
        message_id=identifier, source_type="dms_card", owner_open_id="ou_owner",
        source_data=_metadata(parsed, "cli_dms", "oc_dms"),
        chat_id="oc_dms", chat_name="DBA", text="DMS 数据导出申请",
        source_created_at=created, source_hash=parsed["hash"],
        due_at=created + timedelta(minutes=15), status="waiting",
        model_result=_presentation(parsed), reasons=["卡片仍然待审批。"],
        created_at=created, updated_at=created,
    )


def add_dms_attention(system, *, created=None, with_attention=True, buttons=("同意", "拒绝")):
    created = created or system.clock.now
    raw = dms_card(created, buttons=buttons)
    row = dms_attention_row(raw, created)
    with system.sessions.begin() as db:
        db.add(Chat(
            chat_id="oc_dms", name="DBA", chat_mode="p2p", raw={"chat_type": "p2p"},
            generation="test", visible=True, synced_at=system.clock.now,
        ))
        db.add(Message(
            message_id=raw["message_id"], chat_id=raw["chat_id"], msg_type=raw["msg_type"],
            sender_name="DBA", text="DMS 数据导出申请", create_time=int(raw["create_time"]),
            deleted=False, raw=raw, synced_at=system.clock.now,
        ))
        if with_attention:
            db.add(row)
    return row.message_id


def complete_dms_card(system, identifier, *, update_attention=True):
    with system.sessions.begin() as db:
        message = db.get(Message, "om_dms")
        raw = dms_card(
            system.clock.now, buttons=("已同意",), create_time=message.raw["create_time"],
        )
        parsed = _parse(raw)
        message.raw, message.synced_at = raw, system.clock.now
        if update_attention:
            row = db.get(MessageAttention, identifier)
            row.status = "responded"
            row.source_data = _metadata(parsed, "cli_dms", "oc_dms")
            row.source_hash = parsed["hash"]
            row.updated_at = system.clock.now


def notice_rows(system):
    with system.sessions() as db:
        return db.scalars(select(StatusNotification).order_by(
            StatusNotification.created_at, StatusNotification.id,
        )).all()


def sent_texts(system):
    return [item[0] for item in system.feishu.sent]


def sent_phases(system):
    phases = {row.id: row.phase for row in notice_rows(system)}
    return [phases[identifier] for _, identifier, _ in system.feishu.sent]


def test_activation_baselines_cached_approvals_and_attention_without_history_spam(notices):
    system = notices
    add_pending(system)
    add_mention(system, created=system.clock.now - timedelta(hours=1))
    add_attention(system)
    activate(system)
    for _ in range(3):
        system.service.tick()
    assert not system.feishu.sent
    system.service = StatusNotificationService(system.settings, system.sessions, system.feishu)
    system.service.tick()
    assert not system.feishu.sent


def test_personal_mention_is_reported_before_model_assessment(notices):
    activate(notices)
    add_mention(notices)
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    text, _, owner = notices.feishu.sent[0]
    assert owner == "ou_owner" and "业务群" in text and "正在判断" in text
    with notices.sessions() as db:
        assert db.get(MessageAttention, "om_mention") is None
    assert notice_rows(notices)[0].status == "sent"


def test_pending_approval_is_reported_before_model_assessment(notices):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert "测试审批 task_1" in sent_texts(notices)[0]
    with notices.sessions() as db:
        assert db.get(ApprovalReview, "task_1") is None


@pytest.mark.parametrize("changes", [
    {"mentions": [{"id": "all", "id_type": "open_id"}]},
    {"mentions": [{"id": "ou_other", "id_type": "open_id"}]},
    {"sender": {"id": "ou_owner", "id_type": "open_id", "sender_type": "user"}},
    {"sender": {"id": "cli_test", "id_type": "app_id", "sender_type": "app"}},
    {"deleted": True},
])
def test_other_messages_do_not_generate_owner_status_notices(notices, changes):
    activate(notices)
    add_mention(notices, changes=changes)
    notices.service.tick()
    assert not notices.feishu.sent


def test_hidden_chat_historical_message_and_other_persons_approval_are_not_reported(notices):
    activate(notices)
    add_mention(notices, "om_old", created=notices.clock.now - timedelta(days=1))
    notices.service.tick()
    add_mention(notices, "om_hidden")
    with notices.sessions.begin() as db:
        db.get(Chat, "oc_team").visible = False
    add_pending(notices, owner="ou_other")
    notices.service.tick()
    assert not notices.feishu.sent


@pytest.mark.parametrize("important", [True, False])
def test_judgment_change_reports_both_important_and_ordinary_mentions_once(notices, important):
    activate(notices)
    add_mention(notices)
    notices.service.tick()
    notices.clock.now += timedelta(seconds=1)
    add_attention(notices, important=important)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    expected = "重要消息，待你响应" if important else "无需响应"
    assert expected in sent_texts(notices)[-1]
    for _ in range(3):
        notices.clock.now += timedelta(seconds=1)
        notices.service.tick()
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert len({item[1] for item in notices.feishu.sent}) == 2


def test_approval_needing_owner_and_confirmed_approval_each_get_a_status_update(notices):
    activate(notices)
    add_pending(notices)
    notices.service.tick()
    notices.clock.now += timedelta(seconds=1)
    add_human_review(notices)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert "验收" in sent_texts(notices)[-1]
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        review = db.get(ApprovalReview, "task_1")
        review.decision = "eligible"
        review.execution_status = "approved"
        review.submitted_at = notices.clock.now
        review.confirmed_at = notices.clock.now
        review.reviewed_at = notices.clock.now
    notices.service.tick()
    assert len(notices.feishu.sent) == 3
    assert "已通过" in sent_texts(notices)[-1]
    notices.service.tick()
    assert len(notices.feishu.sent) == 3


def test_model_recommendation_to_approve_is_not_reported_as_success(notices):
    activate(notices)
    add_pending(notices)
    add_human_review(notices)
    with notices.sessions.begin() as db:
        review = db.get(ApprovalReview, "task_1")
        assessment = db.get(ModelAssessment, review.assessment_id)
        result = {**assessment.result, "decision": "approve", "eligible": True}
        assessment.result = result
        review.model_review = {**review.model_review, **result}
        review.decision = "eligible"
        review.execution_status = "awaiting_review"
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert "待提交" in sent_texts(notices)[0]
    assert "已通过" not in sent_texts(notices)[0]
    assert notice_rows(notices)[0].phase == "waiting"


def test_dms_pending_and_completed_cards_report_status_without_approving(notices):
    activate(notices)
    identifier = add_dms_attention(notices)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert sent_phases(notices) == ["received", "needs_human"]
    assert "123456" in sent_texts(notices)[0]
    assert "待你审批" in sent_texts(notices)[-1]
    notices.clock.now += timedelta(seconds=1)
    complete_dms_card(notices, identifier)
    notices.service.tick()
    assert len(notices.feishu.sent) == 3
    assert sent_phases(notices) == ["received", "needs_human", "responded"]
    assert "已通过" in sent_texts(notices)[-1]
    notices.service.tick()
    assert len(notices.feishu.sent) == 3


def test_pending_dms_message_is_reported_before_attention_then_shared_state_deduplicates(notices):
    activate(notices)
    identifier = add_dms_attention(notices, with_attention=False)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert sent_phases(notices) == ["received", "needs_human"]
    assert "123456" in sent_texts(notices)[0] and "待你审批" in sent_texts(notices)[-1]
    with notices.sessions.begin() as db:
        assert db.get(MessageAttention, identifier) is None
        message = db.get(Message, "om_dms")
        db.add(dms_attention_row(message.raw, notices.clock.now))
    notices.clock.now += timedelta(seconds=1)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert len(notice_rows(notices)) == 2

    notices.clock.now += timedelta(seconds=1)
    duplicate = dms_card(notices.clock.now, "om_dms_duplicate")
    with notices.sessions.begin() as db:
        db.add(Message(
            message_id=duplicate["message_id"], chat_id=duplicate["chat_id"],
            msg_type=duplicate["msg_type"], sender_name="DBA", text="DMS 数据导出申请",
            create_time=int(duplicate["create_time"]), deleted=False,
            raw=duplicate, synced_at=notices.clock.now,
        ))
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    for _ in range(3):
        notices.service.tick()
    assert len(notices.feishu.sent) == 2
    assert sent_phases(notices) == ["received", "needs_human"]
    assert len(notice_rows(notices)) == 2


def test_unknown_dms_buttons_still_get_receipt_before_unverified_status(notices):
    activate(notices)
    identifier = add_dms_attention(notices, with_attention=False, buttons=("查看详情",))
    notices.service.tick()
    assert sent_phases(notices) == ["received", "error"]
    assert all("待你审批" not in text for text in sent_texts(notices))
    with notices.sessions() as db:
        assert db.get(MessageAttention, identifier) is None


def test_dms_receipt_survives_permission_wait_and_precedes_latest_completed_status(notices):
    activate(notices)
    notices.feishu.ready = False
    identifier = add_dms_attention(notices, with_attention=False)
    notices.service.tick()
    receipt = next(row for row in notice_rows(notices) if row.phase == "received")
    assert receipt.submitted_at is None and not notices.feishu.sent

    notices.clock.now += timedelta(seconds=1)
    complete_dms_card(notices, identifier, update_attention=False)
    notices.service.tick()
    rows = notice_rows(notices)
    current_receipt = next(row for row in rows if row.id == receipt.id)
    assert current_receipt.status != "superseded" and current_receipt.submitted_at is None
    assert all(row.status == "superseded" for row in rows if row.phase == "needs_human")
    assert not notices.feishu.sent

    notices.clock.now += timedelta(seconds=61)
    notices.feishu.ready = True
    notices.service.tick()
    assert sent_phases(notices) == ["received", "responded"]
    assert notices.feishu.sent[0][1] == receipt.id
    assert "已通过" in sent_texts(notices)[-1]
    notices.service.tick()
    assert len(notices.feishu.sent) == 2


def test_new_dms_conclusion_cannot_bypass_receipt_permission_retry_delay(notices):
    activate(notices)
    notices.feishu.ready = False
    identifier = add_dms_attention(notices, with_attention=False)
    notices.service.tick()
    receipt = next(
        row for row in notice_rows(notices) if row.revision == dms_receipt_revision(identifier)
    )
    assert receipt.next_attempt_at > notices.clock.now and receipt.submitted_at is None

    notices.clock.now += timedelta(seconds=1)
    notices.feishu.ready = True
    complete_dms_card(notices, identifier, update_attention=False)
    notices.service.tick()
    conclusion = next(row for row in notice_rows(notices) if row.phase == "responded")
    # The new conclusion is due now, while the receipt is absent from this
    # tick's send candidates. Only the explicit prerequisite gate can stop it.
    assert conclusion.next_attempt_at is None and conclusion.status == "queued"
    assert conclusion.submitted_at is None and not notices.feishu.sent
    assert notices.clock.now < receipt.next_attempt_at

    notices.clock.now = receipt.next_attempt_at
    notices.service.tick()
    assert sent_phases(notices) == ["received", "responded"]
    assert notices.feishu.sent[0][1] == receipt.id


def test_unknown_dms_card_creation_blocks_conclusion_without_duplicate_on_restart(notices):
    activate(notices)
    identifier = add_dms_attention(notices, with_attention=False)
    revision = dms_receipt_revision(identifier)

    def fail_only_receipt(text, notice_id):
        with notices.sessions() as db:
            notice = db.get(StatusNotification, notice_id)
            notices.feishu.send_error = FeishuError("network") if notice.revision == revision else None

    notices.feishu.before_send = fail_only_receipt
    notices.service.tick()
    rows = notice_rows(notices)
    receipt = next(row for row in rows if row.revision == revision)
    conclusion = next(row for row in rows if row.phase == "needs_human")
    assert receipt.status == "unknown" and receipt.submitted_at is not None
    assert receipt.sent_at is None
    assert conclusion.status == "blocked_unknown" and conclusion.sent_at is None
    assert sent_phases(notices) == ["received"]

    notices.clock.now += timedelta(hours=1)
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    for _ in range(3):
        notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert sum(notice_id == receipt.id for _, notice_id, _ in notices.feishu.sent) == 1
    assert next(row for row in notice_rows(notices) if row.id == receipt.id).status == "unknown"


def test_old_dms_raw_discovered_by_later_attention_does_not_get_a_new_receipt(notices):
    activate(notices)
    identifier = add_dms_attention(
        notices, with_attention=False, created=notices.clock.now - timedelta(hours=1),
    )
    notices.service.tick()
    assert not notices.feishu.sent
    notices.clock.now += timedelta(seconds=1)
    with notices.sessions.begin() as db:
        message = db.get(Message, "om_dms")
        db.add(dms_attention_row(message.raw, notices.clock.now))
    notices.service.tick()
    assert sent_phases(notices) == ["needs_human"]
    assert not any(row.phase == "received" for row in notice_rows(notices))
    with notices.sessions() as db:
        assert db.get(MessageAttention, identifier).status == "waiting"


def test_new_completed_dms_card_without_attention_gets_receipt_then_actual_completion(notices):
    activate(notices)
    identifier = add_dms_attention(notices, with_attention=False, buttons=("已同意",))
    notices.service.tick()
    assert sent_phases(notices) == ["received", "responded"]
    assert "123456" in sent_texts(notices)[0]
    assert "已通过" in sent_texts(notices)[-1]
    with notices.sessions() as db:
        assert db.get(MessageAttention, identifier) is None
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    notices.service.tick()
    assert len(notices.feishu.sent) == 2


def test_cached_completed_dms_card_never_reports_stale_pending_attention(notices):
    activate(notices)
    identifier = add_dms_attention(notices)
    notices.clock.now += timedelta(seconds=1)
    complete_dms_card(notices, identifier, update_attention=False)
    notices.service.tick()
    assert sent_phases(notices) == ["received", "responded"]
    assert all("待你审批" not in text for text in sent_texts(notices))
    with notices.sessions() as db:
        # Notification projection is read-only; the attention worker owns its state.
        assert db.get(MessageAttention, identifier).status == "waiting"


def test_baselined_old_dms_card_reports_later_completion_without_replaying_pending(notices):
    identifier = add_dms_attention(notices, created=notices.clock.now - timedelta(hours=1))
    activate(notices)
    notices.service.tick()
    assert not notices.feishu.sent
    complete_dms_card(notices, identifier)
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert sent_phases(notices) == ["responded"]
    assert "已通过" in sent_texts(notices)[0]
    assert "待你审批" not in sent_texts(notices)[0]
    notices.service.tick()
    assert len(notices.feishu.sent) == 1


def test_latest_judgment_supersedes_unsent_received_notice(notices):
    activate(notices)
    notices.feishu.ready = False
    add_mention(notices)
    notices.service.tick()
    assert not notices.feishu.sent
    assert any(row.status == "blocked_permission" for row in notice_rows(notices))
    notices.clock.now += timedelta(seconds=61)
    add_attention(notices)
    notices.feishu.ready = True
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert "重要消息，待你响应" in sent_texts(notices)[0]
    rows = notice_rows(notices)
    assert len([row for row in rows if row.status == "superseded"]) == 1
    assert len([row for row in rows if row.status == "sent"]) == 1


@pytest.mark.parametrize("already_submitted", [False, True])
def test_returning_to_superseded_revision_revives_only_a_never_submitted_notice(
    notices, already_submitted,
):
    activate(notices)
    notices.feishu.ready = already_submitted
    add_mention(notices)
    notices.service.tick()
    original = notice_rows(notices)[0]
    assert (original.submitted_at is not None) is already_submitted

    notices.feishu.ready = False
    notices.clock.now += timedelta(seconds=61)
    add_attention(notices)
    notices.service.tick()
    rows = notice_rows(notices)
    assert len(rows) == 2
    assert next(row for row in rows if row.id == original.id).status == (
        "sent" if already_submitted else "superseded"
    )

    notices.clock.now += timedelta(seconds=61)
    with notices.sessions.begin() as db:
        row = db.get(MessageAttention, "om_mention")
        row.status = "queued"
        row.model_result = {}
        row.updated_at = notices.clock.now
    notices.feishu.ready = True
    notices.service.tick()
    # A was either delivered originally or revived now; never delivered twice.
    assert len(notices.feishu.sent) == 1
    assert notices.feishu.sent[0][1] == original.id
    rows = notice_rows(notices)
    assert len(rows) == 2
    assert next(row for row in rows if row.id == original.id).status == "sent"
    assert next(row for row in rows if row.id != original.id).status == "superseded"


def test_each_tick_sends_at_most_three_and_drains_remaining_without_duplicates(notices):
    activate(notices)
    for index in range(5):
        add_mention(notices, f"om_mention_{index}")
    notices.service.tick()
    assert len(notices.feishu.sent) == 3
    notices.service.tick()
    assert len(notices.feishu.sent) == 5
    notices.service.tick()
    assert len(notices.feishu.sent) == 5
    assert len({item[1] for item in notices.feishu.sent}) == 5


def test_submission_is_committed_before_network_and_unknown_delivery_is_not_retried(notices):
    activate(notices)
    add_mention(notices)

    def check_marker(text, identifier):
        with notices.sessions() as db:
            row = db.get(StatusNotification, identifier)
            assert row.status == "sending" and row.submitted_at == notices.clock.now
            assert row.text == text and row.owner_open_id == "ou_owner"
        concurrent = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
        assert concurrent.tick() is None

    notices.feishu.before_send = check_marker
    notices.feishu.send_error = FeishuError("network")
    notices.service.tick()
    rows = notice_rows(notices)
    assert len(rows) == 1 and rows[0].status == "unknown"
    assert rows[0].submitted_at is not None and rows[0].sent_at is None
    notices.feishu.send_error = None
    notices.clock.now += timedelta(hours=1)
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    for _ in range(3):
        notices.service.tick()
    assert len(notices.feishu.sent) == 1


def test_missing_message_permission_waits_without_submission_then_recovers(notices):
    activate(notices)
    notices.feishu.ready = False
    add_pending(notices)
    notices.service.tick()
    rows = notice_rows(notices)
    assert len(rows) == 1 and rows[0].status == "blocked_permission"
    assert rows[0].submitted_at is None and not notices.feishu.sent
    notices.clock.now += timedelta(minutes=2)
    notices.feishu.ready = True
    notices.service.tick()
    assert len(notices.feishu.sent) == 1
    assert notice_rows(notices)[0].status == "sent"


def test_owner_change_never_delivers_a_previous_owners_queued_notice(notices):
    activate(notices)
    notices.feishu.ready = False
    add_mention(notices)
    notices.service.tick()
    with notices.sessions.begin() as db:
        db.get(Account, 1).open_id = "ou_different_owner"
    notices.feishu.ready = True
    notices.clock.now += timedelta(minutes=2)
    with pytest.raises(AppError, match="账号"):
        notices.service.tick()
    notices.service = StatusNotificationService(notices.settings, notices.sessions, notices.feishu)
    with pytest.raises(AppError, match="账号"):
        notices.service.tick()
    assert not notices.feishu.sent
    assert all(row.submitted_at is None for row in notice_rows(notices))


@pytest.mark.parametrize("source_type", ["message", "dms_card"])
def test_notices_never_change_existing_phone_markers_or_global_call_cooldown(notices, source_type):
    activate(notices)
    old_call = notices.clock.now - timedelta(minutes=5)
    if source_type == "message":
        identifier = "om_mention"
        add_mention(notices)
        add_attention(notices)
    else:
        identifier = add_dms_attention(notices)
    with notices.sessions.begin() as db:
        db.add(AttentionState(
            id=1, owner_open_id="ou_owner", activated_at=old_call,
            discovery_at=old_call, discovery_message_id="om_earlier",
            last_call_submitted_at=old_call,
        ))
        row = db.get(MessageAttention, identifier)
        row.reminder_uuid = str(uuid4())
        row.reminder_message_id = "om_existing_reminder"
        row.reminder_submitted_at = row.phone_submitted_at = row.phone_accepted_at = old_call
        row.status = "phone_accepted"
        before = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
    notices.service.tick()
    assert len(notices.feishu.sent) == (2 if source_type == "dms_card" else 1)
    if source_type == "dms_card":
        assert sent_phases(notices) == ["received", "phone_accepted"]
    with notices.sessions() as db:
        row = db.get(MessageAttention, identifier)
        after = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        assert after == before
        state = db.get(AttentionState, 1)
        assert state.last_call_submitted_at == old_call
        assert state.discovery_message_id == "om_earlier"
        assert state.discovery_at == old_call


def test_disabled_service_does_not_discover_or_send(notices):
    notices.settings.status_notifications_enabled = False
    add_mention(notices)
    add_pending(notices)
    notices.service.tick()
    assert not notices.feishu.sent
    with notices.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)) is None
    assert not notice_rows(notices)
