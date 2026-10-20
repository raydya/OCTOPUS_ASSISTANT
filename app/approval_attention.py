"""Read-only approval source for the shared, at-most-once reminder delivery.

The source never approves, sends messages, or submits calls. Discovery starts a
new grace period only for a new verified assessment. Delivery rechecks both live
Feishu state and local human feedback before committing either outbound marker.
"""

import hashlib
import re
from datetime import timedelta

from sqlalchemy import select

from app.errors import AppError
from app.model_review import business_snapshot
from app.models import (
    Account,
    ApprovalFeedback,
    ApprovalReview,
    ApprovalTask,
    MessageAttention,
    ModelAssessment,
    utcnow,
)

TASK_PAGE_LIMIT = 10
TERMINAL_INSTANCE_STATES = {"APPROVED", "REJECTED", "CANCELED", "DELETED", "REVERTED"}


def approval_attention_id(task_id):
    return "approval_" + hashlib.sha256(task_id.encode()).hexdigest()


def _result(status, reason, error=None):
    return status, [reason], error


def _refresh(db, model, key):
    # The caller may already hold row locks. Always refresh its ORM identity
    # map so a previously loaded review cannot hide a newly committed answer.
    return db.get(model, key, populate_existing=True)


def _task_check(task, task_id, instance_code, owner):
    if (
        not isinstance(task, dict)
        or task.get("task_id") != task_id
        or task.get("instance_code") != instance_code
    ):
        return _result("blocked", "无法核实审批待办与实例的对应关系。", "approval_attention_identity")
    if not task.get("user_id"):
        return _result("blocked", "无法核实当前审批人的身份。", "approval_attention_owner_unknown")
    if task["user_id"] != owner:
        return _result("responded", "审批已转交给其他人，停止本轮提醒。")
    for field in ("status", "topic", "instance_status"):
        if task.get(field) is None:
            return _result("blocked", "审批待办状态不完整，未触发电话。", "approval_attention_state_unknown")
        if str(task[field]) != "1":
            return _result("responded", "审批已不再是本人的待处理任务，停止本轮提醒。")
    return None, [], None


def _detail_check(task, detail, task_id, instance_code, owner):
    if (
        not isinstance(detail, dict)
        or detail.get("instance_code") != instance_code
        or not detail.get("definition_code")
        or detail["definition_code"] != task.get("definition_code")
    ):
        return _result("blocked", "审批详情与待办的身份或定义不一致。", "approval_attention_identity")
    if detail.get("reverted") is True or detail.get("status") in TERMINAL_INSTANCE_STATES:
        return _result("responded", "审批已完成或撤回，停止本轮提醒。")
    if detail.get("status") != "PENDING" or detail.get("reverted") is not False:
        return _result("blocked", "未能确认审批仍然待处理且未撤回。", "approval_attention_state_unknown")
    tasks, nodes = detail.get("tasks"), detail.get("current_nodes")
    if not isinstance(tasks, list) or not isinstance(nodes, list):
        return _result("blocked", "审批任务或当前节点信息不完整。", "approval_attention_state_unknown")
    current = [item for item in tasks if isinstance(item, dict) and item.get("id") == task_id]
    if len(current) != 1:
        return _result("blocked", "审批详情中未能唯一确认该任务。", "approval_attention_identity")
    current = current[0]
    if not current.get("user_id") or not current.get("status"):
        return _result("blocked", "审批详情中缺少任务身份或状态。", "approval_attention_state_unknown")
    if current["user_id"] != owner or current["status"] != "PENDING":
        return _result("responded", "当前用户的审批任务已完成或转交，停止本轮提醒。")
    active = [
        (node, approver)
        for node in nodes if isinstance(node, dict) and isinstance(node.get("approvers"), list)
        for approver in node["approvers"]
        if isinstance(approver, dict) and approver.get("task_id") == task_id
    ]
    if (
        len(active) != 1 or not current.get("node_id")
        or active[0][0].get("node_id") != current["node_id"]
        or not active[0][1].get("user_id")
    ):
        return _result("blocked", "当前审批节点未能确认该任务，未触发电话。", "approval_attention_node_unknown")
    if active[0][1]["user_id"] != owner:
        return _result("responded", "当前节点已不再由本人审批，停止本轮提醒。")
    return None, [], None


class ApprovalAttentionSource:
    def __init__(self, settings, sessions, feishu):
        self.settings, self.sessions, self.feishu = settings, sessions, feishu

    @staticmethod
    def _inputs(db, task_id, owner):
        """Return current, mutually bound local facts, without remote requests."""
        account = _refresh(db, Account, 1)
        if not account or account.open_id != owner:
            return None, _result("blocked", "提醒绑定的账号已无法确认。", "approval_attention_owner_unknown")
        task = _refresh(db, ApprovalTask, (task_id, "1"))
        review = _refresh(db, ApprovalReview, task_id)
        if not task or not task.visible:
            return None, _result("responded", "本地待审批列表已无此任务，停止本轮提醒。")
        problem = _task_check(task.raw, task_id, task.instance_code, owner)
        if problem[0]:
            return None, problem
        if not review or review.owner_open_id != owner or review.instance_code != task.instance_code:
            return None, _result("blocked", "审批评估与当前用户或任务不匹配。", "approval_attention_identity")
        if (
            review.submitted_at is not None
            or review.decision != "needs_review"
            or review.execution_status != "awaiting_review"
        ):
            return None, _result("responded", "审批已进入其他处理状态，不再等待本人介入。")
        model = review.model_review
        if not isinstance(model, dict):
            return None, _result("blocked", "尚无可核实的人工介入评估。", "approval_attention_assessment_missing")
        latest = db.scalar(
            select(ApprovalFeedback).where(
                ApprovalFeedback.task_id == task_id, ApprovalFeedback.owner_open_id == owner,
            ).order_by(ApprovalFeedback.created_at.desc(), ApprovalFeedback.id.desc()).limit(1)
        )
        if model.get("held") or (latest and latest.action == "hold"):
            return None, _result("responded", "本人已暂缓该审批，停止电话提醒。")
        if model.get("feedback_pending"):
            return None, _result("responded", "已收到本人反馈，停止本轮提醒并等待重新评估。")
        if model.get("stale"):
            return None, _result("source_changed", "审批内容已变化，等待新的完整评估后重新计时。")
        if (
            model.get("status") != "complete"
            or model.get("decision") != "needs_human"
            or model.get("eligible") is not False
        ):
            return None, _result("responded", "当前评估不再明确要求本人介入，停止本轮提醒。")
        if (
            not isinstance(review.snapshot_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", review.snapshot_hash)
            or not review.assessment_id
            or model.get("assessment_id") != review.assessment_id
            or model.get("snapshot_hash") != review.snapshot_hash
        ):
            return None, _result("blocked", "评估快照绑定不完整，未触发电话。", "approval_attention_assessment_invalid")
        assessment = _refresh(db, ModelAssessment, review.assessment_id)
        if (
            not assessment or assessment.task_id != task_id
            or assessment.instance_code != task.instance_code
            or assessment.owner_open_id != owner
            or assessment.snapshot_hash != review.snapshot_hash
            or assessment.status != "complete"
            or not isinstance(assessment.result, dict)
            or assessment.result.get("decision") != "needs_human"
            or assessment.result.get("eligible") is not False
            or assessment.result.get("held")
            or assessment.model != "gpt-6-sol"
            or assessment.reasoning_effort != "xhigh"
        ):
            return None, _result("blocked", "原始模型评估尚不完整或绑定已失效。", "approval_attention_assessment_invalid")
        if (latest.id if latest else None) != assessment.feedback_id:
            return None, _result("responded", "已有尚未纳入本轮评估的本人反馈，停止本轮提醒。")
        return (task, review, assessment), (None, [], None)

    def local_check(self, db, row):
        """The delivery caller holds the review lock through marker commit."""
        if row.source_type != "approval" or not row.approval_task_id:
            return _result("blocked", "审批提醒来源无效。", "approval_attention_identity")
        if row.acknowledged_at:
            return _result("responded", "本人已停止本次提醒。")
        inputs, problem = self._inputs(db, row.approval_task_id, row.owner_open_id)
        if problem[0]:
            return problem
        task, review, assessment = inputs
        if (
            row.approval_instance_code != task.instance_code
            or row.approval_assessment_id != assessment.id
            or row.source_hash != review.snapshot_hash
        ):
            return _result("source_changed", "审批内容或模型评估已更新，需重新计满 15 分钟。")
        return None, [], None

    def discover(self, db, state, now, set_status):
        """Called in the attention leader transaction; does not call the model."""
        now = now or utcnow()
        existing = db.scalars(select(MessageAttention).where(
            MessageAttention.source_type == "approval",
            MessageAttention.owner_open_id == state.owner_open_id,
        ).with_for_update()).all()
        by_task = {row.approval_task_id: row for row in existing}
        for row in existing:
            if row.acknowledged_at or row.reminder_submitted_at or row.phone_submitted_at:
                continue
            status, reasons, error = self.local_check(db, row)
            if status and (row.status, row.reasons, row.error_code) != (status, reasons, error):
                set_status(
                    row, status, reasons, error,
                    next_attempt=now + timedelta(seconds=60) if status == "blocked" else None,
                )
        task_ids = db.scalars(select(ApprovalReview.task_id).where(
            ApprovalReview.owner_open_id == state.owner_open_id,
            ApprovalReview.decision == "needs_review",
            ApprovalReview.execution_status == "awaiting_review",
            ApprovalReview.submitted_at.is_(None),
        )).all()
        for task_id in task_ids:
            inputs, problem = self._inputs(db, task_id, state.owner_open_id)
            if problem[0]:
                continue
            task, review, assessment = inputs
            row = by_task.get(task_id)
            if row:
                if row.acknowledged_at or row.reminder_submitted_at or row.phone_submitted_at:
                    continue
                if (
                    row.source_hash == review.snapshot_hash
                    and row.approval_assessment_id == assessment.id
                    and row.approval_instance_code == task.instance_code
                ):
                    continue
            else:
                row = MessageAttention(
                    message_id=approval_attention_id(task_id), source_type="approval",
                    owner_open_id=state.owner_open_id, approval_task_id=task_id,
                    chat_id="", assessment_cache={}, assessment_context=[],
                )
                db.add(row)
                by_task[task_id] = row
            result = assessment.result
            questions = result.get("questions") if isinstance(result.get("questions"), list) else []
            questions = [item for item in questions if isinstance(item, str) and item.strip()]
            summary = result.get("summary") if isinstance(result.get("summary"), str) else ""
            reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
            row.approval_instance_code = task.instance_code
            row.approval_assessment_id = assessment.id
            row.chat_name = task.title
            row.text = "\n".join([summary, *questions]).strip()
            row.source_hash, row.context_hash = review.snapshot_hash, None
            row.source_created_at = now
            row.due_at = now + timedelta(seconds=self.settings.attention_timeout_seconds)
            row.assessed_at = assessment.updated_at
            row.attempts = 0
            row.model_result = {
                "decision": "needs_human",
                "summary": summary, "questions": questions, "reasons": reasons,
                "evidence": result.get("evidence", []),
                "model": assessment.model, "reasoning_effort": assessment.reasoning_effort,
                "source": "approval", "risk": result.get("risk"),
                "assessment_id": assessment.id,
            }
            set_status(
                row, "waiting",
                ["该审批需要本人介入；从本次评估发现时起等待 15 分钟，收到反馈后停止提醒。"],
                next_attempt=row.due_at,
            )

    def fresh_check(self, row):
        with self.sessions() as db:
            status, reasons, error = self.local_check(db, row)
            if status:
                return status, reasons, error
        try:
            task, token, seen = None, None, set()
            for _ in range(TASK_PAGE_LIMIT):
                listing = self.feishu.tasks("1", token)
                tasks = listing.get("tasks") if isinstance(listing, dict) else None
                if not isinstance(tasks, list) or any(not isinstance(item, dict) for item in tasks):
                    return _result("blocked", "飞书待审批列表不完整，未触发电话。", "approval_attention_tasks_invalid")
                matches = [item for item in tasks if item.get("task_id") == row.approval_task_id]
                if len(matches) > 1:
                    return _result("blocked", "飞书待审批任务标识重复，未触发电话。", "approval_attention_identity")
                if matches:
                    task = matches[0]
                    break
                if listing.get("has_more") is False:
                    return _result("responded", "飞书最新完整待审批列表已无此任务，停止本轮提醒。")
                token = listing.get("page_token")
                if listing.get("has_more") is not True or not isinstance(token, str) or not token or token in seen:
                    return _result("blocked", "未能完整读取飞书待审批列表，未触发电话。", "approval_attention_tasks_incomplete")
                seen.add(token)
            if task is None:
                return _result("blocked", "待审批列表尚未完整核验，未触发电话。", "approval_attention_tasks_incomplete")
            problem = _task_check(
                task, row.approval_task_id, row.approval_instance_code, row.owner_open_id,
            )
            if problem[0]:
                return problem
            detail = self.feishu.instance(row.approval_instance_code)
            problem = _detail_check(
                task, detail, row.approval_task_id, row.approval_instance_code, row.owner_open_id,
            )
            if problem[0]:
                return problem
            if business_snapshot(task, detail, row.owner_open_id) != row.source_hash:
                return _result("source_changed", "飞书审批内容已变化，等待新评估后重新计时。")
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "approval_attention_unavailable"
            if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_:-]{1,64}", code):
                code = "approval_attention_unavailable"
            return _result("blocked", "未能完成审批实时核验，未触发电话。", code)
        with self.sessions() as db:
            # Feedback committed during the remote reads also cancels delivery.
            return self.local_check(db, row)

    def reminder_text(self, row):
        summary = str((row.model_result or {}).get("summary") or "")[:1000]
        questions = (row.model_result or {}).get("questions") or []
        questions = "\n".join(
            f"• {item[:500]}" for item in questions[:10] if isinstance(item, str)
        )
        return (
            f"审批待你介入：{row.chat_name[:300]}\n"
            f"发现需要你确认的审批后已等待 {self.settings.attention_timeout_seconds // 60} 分钟，"
            "尚未收到处理或补充信息。\n"
            f"{summary}\n{questions}\n"
            "请在飞书审批中处理，或在工作台补充信息、确认通过、暂缓或停止本次提醒。"
        )
