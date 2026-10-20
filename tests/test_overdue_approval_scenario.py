from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_approval_attention import approval_system as approval_system
from test_approval_attention import (
    attention_record,
    discover_human_approval,
    fresh_local_pending,
)
from test_approval_review import review_setup as review_setup
from test_model_review import model_result
from test_model_review import setup_model as setup_model

from app.models import ApprovalReview, MessageAttention

pytestmark = pytest.mark.integration


def test_human_review_waiting_sixteen_minutes_sends_one_reminder_and_phone(approval_system):
    system = approval_system
    discovered = discover_human_approval(system)
    first_assessment = discovered.approval_assessment_id
    system.clock.now = discovered.due_at + timedelta(minutes=1)
    # Keep synchronization fresh so the second tick actually re-examines this
    # old approval instead of stopping at the ten-minute snapshot safety gate.
    fresh_local_pending(system)
    reads_before = system.feishu.reads
    result = system.reviewer.tick()
    assert result["execution_status"] == "awaiting_review"
    assert result["decision"] == "needs_review"
    assert result["assessment_id"] == first_assessment
    assert result["model_review"]["questions"] == ["请提供该申请的验收结论。"]
    assert result["reviewed_at"] == system.clock.now
    assert system.feishu.reads > reads_before
    assert len(system.provider.calls) == 1  # Unchanged material uses the saved judgment.
    reminder = system.service.tick()
    assert reminder["status"] == "phone_accepted"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.feishu.posts
    assert not system.model.calls  # Approval judgment is reused without another model call.
    assert attention_record(system).phone_submitted_at is not None
    with system.sessions() as db:
        review = db.get(ApprovalReview, "task_1")
        assert review.submitted_at is None
        assert db.scalar(select(func.count()).select_from(MessageAttention)) == 1
    print(
        "审批模拟：需要人工介入，已等待16分钟；仍为awaiting_review，"
        "工作台保留问题；审批提交0次、本人私聊提醒1次、电话请求1次，模拟飞书已受理。"
    )


def test_low_risk_model_approval_does_not_wait_for_the_message_fifteen_minute_threshold(
    approval_system,
):
    system = approval_system
    system.provider.result = model_result()
    created_at = system.clock.now - timedelta(seconds=30)
    system.feishu.pending[0]["create_time"] = str(int(created_at.timestamp() * 1000))

    result = system.reviewer.tick()
    system.service.tick()

    assert system.clock.now - created_at < timedelta(minutes=15)
    assert result["execution_status"] == "approved"
    assert result["decision"] == "eligible"
    assert result["submitted_at"] == system.clock.now
    assert result["confirmed_at"] == system.clock.now
    assert len(system.provider.calls) == 1
    assert len(system.feishu.posts) == 1
    assert system.feishu.details["instance_task_1"]["tasks"][0]["status"] == "APPROVED"
    assert not system.feishu.sent and not system.feishu.calls
    with system.sessions() as db:
        assert db.scalar(select(func.count()).select_from(MessageAttention)) == 0
    print(
        "审批模拟：申请仅30秒，模型判断低风险且资格完整；本轮模拟通过并回读确认，"
        "无需等待15分钟；私聊0次、电话0次。"
    )
