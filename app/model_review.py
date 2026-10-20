"""Durable model decisions: immutable input identities and explicit human overrides.

Applicant text never grants authority. A model's answer can only pass a bounded
low-risk gate. Human approval is tied to the exact task, owner, and full snapshot.
"""

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text

from app.errors import AppError
from app.models import Account, ApprovalFeedback, ApprovalReview, ModelAssessment, utcnow

MODEL = "gpt-6-sol"
EFFORT = "xhigh"
PROMPT_VERSION = "approval-v1"
MAX_ATTEMPTS = 3


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def business_snapshot(task, detail, owner):
    """Bind all API-supplied business fields, including notes and attachments."""
    return hashlib.sha256(_json({"task": task, "detail": detail, "owner": owner}).encode()).hexdigest()


def _detail_data(detail):
    result = json.loads(_json(detail))
    if isinstance(result.get("form"), str):
        try:
            result["form"] = json.loads(result["form"])
        except (ValueError, TypeError):
            pass
    return result


def _hard_gates(task, detail):
    detail = _detail_data(detail)
    data = _json({"task": task, "detail": detail})
    reasons = []
    categories = [
        (r"转账|付款|支付|报销|借贷|贷款|担保|合同|法务|诉讼|\b(payment|transfer funds|loan|contract)\b",
         "涉及资金、债务或法律承诺，需要本人确认。"),
        (r"招聘|录用|解雇|辞退|裁员|薪酬|薪资|调薪|\b(hiring|dismissal|salary|payroll)\b",
         "涉及人事或薪酬决定，需要本人确认。"),
        (r"下线|删除|销毁|停用|清库|drop\s+table|truncate\s+table|\b(decommission|delete|destroy)\b",
         "涉及下线、停用或数据删除，需要本人确认。"),
        (r"生产环境|生产发布|生产变更|\bproduction\b",
         "涉及生产环境变更，需要本人确认影响和回退依据。"),
        (r"管理员|超级用户|敏感权限|root权限|数据库权限|密钥|口令|机密|个人信息|客户数据|\b(admin|superuser|credential|secret)\b",
         "涉及敏感权限、凭据或敏感数据，需要本人确认。"),
        (r"忽略.{0,15}(指令|规则|提示)|无需.{0,8}(核验|审批).{0,8}直接|system\s*prompt|ignore.{0,20}instructions",
         "申请材料包含影响审批判断的指令性文字，需要人工核验原始业务事实。"),
    ]
    for pattern, reason in categories:
        if re.search(pattern, data, re.IGNORECASE):
            reasons.append(reason)
    form = detail.get("form")
    try:
        if isinstance(form, str):
            form = json.loads(form)
        if not isinstance(form, list) or not form:
            raise ValueError
        supported = {
            "text", "input", "textarea", "number", "amount", "date", "dateInterval",
            "radioV2", "checkboxV2", "contactV2", "departmentV2", "telephone", "email", "address",
        }
        if any(not isinstance(field, dict) or field.get("type") not in supported for field in form):
            reasons.append("表单包含附件或尚未解析的控件，不能将其视作已经核验的证据。")
    except (ValueError, TypeError):
        reasons.append("审批表单不完整，不能自动通过。")
    # Attachments outside the form also require a real content reader.
    def has_attachment(value):
        if isinstance(value, dict):
            return any(
                (key.lower() in {"attachments", "attachment", "files", "file_token", "file_url"}
                 and bool(child)) or has_attachment(child)
                for key, child in value.items()
            )
        return isinstance(value, list) and any(has_attachment(child) for child in value)

    if has_attachment(detail):
        reasons.append("审批包含附件，目前未读取附件内容，需要本人核验。")
    return list(dict.fromkeys(reasons))


def _safe_result(raw, payload, gates):
    """Defence in depth after the provider's strict JSON-schema validation."""
    required = {"decision", "risk", "summary", "reasons", "questions", "evidence", "missing_evidence"}
    if not isinstance(raw, dict) or not required <= raw.keys():
        raise AppError("model_invalid_response", "模型判断格式无效。")
    if raw["decision"] not in {"approve", "needs_human"} or raw["risk"] not in {"low", "medium", "high"}:
        raise AppError("model_invalid_response", "模型判断格式无效。")
    if not isinstance(raw["summary"], str) or not 1 <= len(raw["summary"]) <= 4000:
        raise AppError("model_invalid_response", "模型判断格式无效。")
    for key in ("reasons", "questions", "missing_evidence"):
        if (
            not isinstance(raw[key], list) or len(raw[key]) > 30
            or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in raw[key])
        ):
            raise AppError("model_invalid_response", "模型判断格式无效。")
    if not isinstance(raw["evidence"], list) or len(raw["evidence"]) > 30:
        raise AppError("model_invalid_response", "模型证据格式无效。")
    sources = {
        **payload["untrusted_approval"], "human_feedback": payload["human_feedback"],
    }
    valid_evidence = []
    for item in raw["evidence"]:
        if (
            not isinstance(item, dict) or set(item) != {"source", "quote"}
            or item["source"] not in sources or not isinstance(item["quote"], str)
            or not 1 <= len(item["quote"]) <= 2000
        ):
            raise AppError("model_invalid_response", "模型证据格式无效。")
        # A verbatim quote must actually occur in its claimed input source.
        source = sources[item["source"]]
        if not any(item["quote"] in representation for representation in (
            _json(source), json.dumps(source, ensure_ascii=False), str(source),
        )):
            raise AppError("model_unverified_evidence", "模型引用了无法核实的证据。")
        valid_evidence.append(item)
    reasons = [*raw["reasons"], *gates]
    if not valid_evidence:
        reasons.append("模型未提供可定位的业务依据。")
    eligible = (
        raw["decision"] == "approve" and raw["risk"] == "low"
        and not raw["questions"] and not raw["missing_evidence"] and bool(valid_evidence)
        and not gates
    )
    return {
        "decision": "approve" if eligible else "needs_human",
        "risk": "high" if gates else raw["risk"], "summary": raw["summary"],
        "reasons": reasons, "questions": raw["questions"],
        "evidence": valid_evidence, "missing_evidence": raw["missing_evidence"],
        "eligible": eligible, "source": "model",
    }


def assessment_view(row):
    result = {
        "decision": "needs_human", "risk": "high", "summary": "等待模型判断。",
        "reasons": [], "questions": [], "evidence": [], "missing_evidence": [],
        "eligible": False, "source": "model", **(row.result or {}),
    }
    result.update({
        "assessment_id": row.id, "task_id": row.task_id, "snapshot_hash": row.snapshot_hash,
        "feedback_id": row.feedback_id, "status": row.status,
        "model": row.model, "reasoning_effort": row.reasoning_effort,
        "prompt_version": row.prompt_version, "usage": row.usage,
        "attempts": row.attempts, "error_code": row.error_code,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    })
    if row.status != "complete":
        result["eligible"] = False
    return result


class ModelReviewService:
    def __init__(self, settings, sessions, provider):
        self.settings, self.sessions, self.provider = settings, sessions, provider

    @staticmethod
    def _feedback(db, task_id):
        return db.scalar(
            select(ApprovalFeedback).where(ApprovalFeedback.task_id == task_id)
            .order_by(ApprovalFeedback.created_at.desc(), ApprovalFeedback.id.desc()).limit(1)
        )

    def current(self, task_id):
        with self.sessions() as db:
            review = db.get(ApprovalReview, task_id)
            if review and review.assessment_id:
                current = db.get(ModelAssessment, review.assessment_id)
                if current:
                    return assessment_view(current)
            row = db.scalar(
                select(ModelAssessment).where(ModelAssessment.task_id == task_id)
                .order_by(ModelAssessment.created_at.desc(), ModelAssessment.id.desc()).limit(1)
            )
            return assessment_view(row) if row else None

    def evaluate(self, task, detail, owner):
        snapshot = business_snapshot(task, detail, owner)
        task_id = task["task_id"]
        with self.sessions.begin() as db:
            # Short transaction lock prevents competing requests from inserting
            # duplicate cache entries. It is released before the model request.
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"model:{task_id}"})
            feedback = self._feedback(db, task_id)
            feedback_id = feedback.id if feedback else None
            same_feedback = bool(
                feedback and feedback.owner_open_id == owner and feedback.snapshot_hash == snapshot
            )
            key = hashlib.sha256(_json({
                "task_id": task_id, "owner": owner, "snapshot": snapshot,
                "feedback": feedback_id, "model": MODEL, "effort": EFFORT,
                "prompt_version": PROMPT_VERSION,
            }).encode()).hexdigest()
            row = db.scalar(select(ModelAssessment).where(ModelAssessment.cache_key == key).with_for_update())
            if row and (
                row.status == "complete" or row.attempts >= MAX_ATTEMPTS
                or (row.next_attempt_at and row.next_attempt_at > utcnow())
            ):
                return assessment_view(row)
            if row is None:
                row = ModelAssessment(
                    id=str(uuid4()), cache_key=key, task_id=task_id,
                    instance_code=task["instance_code"], owner_open_id=owner,
                    snapshot_hash=snapshot, feedback_id=feedback_id,
                    model=MODEL, reasoning_effort=EFFORT, prompt_version=PROMPT_VERSION,
                    attempts=0, result={}, usage={},
                )
                db.add(row)
            # Hold applies to the task until explicitly answered or approved.
            if feedback and feedback.owner_open_id == owner and (
                feedback.action == "hold" or (feedback.action == "approve" and same_feedback)
            ):
                approved = feedback.action == "approve"
                previous_assessment = db.get(ModelAssessment, feedback.assessment_id)
                row.status = "complete"
                row.result = {
                    "decision": "approve" if approved else "needs_human",
                    "risk": (
                        (previous_assessment.result or {}).get("risk", "high")
                        if previous_assessment else "high"
                    ),
                    "summary": "本人已确认当前内容通过。" if approved else "本人已暂缓处理。",
                    "reasons": [feedback.text or ("本人明确同意。" if approved else "等待本人后续指示。")],
                    "questions": [], "evidence": [], "missing_evidence": [],
                    "eligible": approved, "source": "human", "held": not approved,
                }
                row.updated_at = utcnow()
                db.flush()
                return assessment_view(row)
            answers = db.scalars(
                select(ApprovalFeedback).where(
                    ApprovalFeedback.task_id == task_id, ApprovalFeedback.owner_open_id == owner,
                    ApprovalFeedback.snapshot_hash == snapshot, ApprovalFeedback.action == "answer",
                ).order_by(ApprovalFeedback.created_at)
            ).all()
            payload = {
                "task_id": task_id, "snapshot_hash": snapshot,
                "untrusted_approval": {"task": task, "detail": _detail_data(detail)},
                "human_feedback": [
                    {"action": item.action, "text": item.text, "snapshot_hash": item.snapshot_hash}
                    for item in answers
                ],
            }
            row.attempts += 1
            row.status, row.error_code, row.updated_at = "assessing", None, utcnow()
            lease = max(300, int(getattr(self.settings, "approval_model_timeout_seconds", 180)) + 60)
            row.next_attempt_at = utcnow() + timedelta(seconds=lease)
            db.flush()
            assessment_id = row.id
            claimed_attempt = row.attempts
        try:
            raw = self.provider.assess(payload)
            result = _safe_result(raw, payload, _hard_gates(task, detail))
            usage = raw.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            usage = {key: value for key, value in usage.items() if isinstance(value, int) and value >= 0}
            error = None
        except Exception as exc:
            candidate = exc.code if isinstance(exc, AppError) else "model_unavailable"
            error = candidate if re.fullmatch(r"[a-zA-Z0-9_:-]{1,64}", candidate) else "model_unavailable"
            result, usage = {
                "decision": "needs_human", "risk": "high", "eligible": False,
                "summary": "模型暂时无法完成判断，未提交审批。",
                "reasons": ["模型请求失败或返回结果无法验证；保留待人工处理。"],
                "questions": [], "evidence": [], "missing_evidence": [], "source": "model",
            }, {}
        with self.sessions.begin() as db:
            row = db.get(ModelAssessment, assessment_id, with_for_update=True)
            if row.attempts != claimed_attempt or row.status != "assessing":
                # A reclaimed lease owns the result now. A late response from
                # an older attempt cannot overwrite that newer persisted state.
                return assessment_view(row)
            row.result, row.usage, row.error_code, row.updated_at = result, usage, error, utcnow()
            row.status = "failed" if error else "complete"
            row.next_attempt_at = (
                utcnow() + timedelta(seconds=60 * 2 ** row.attempts)
                if error and row.attempts < MAX_ATTEMPTS else None
            )
            db.flush()
            return assessment_view(row)

    def feedback_matches(self, db, task_id, assessment):
        """Called under the review row lock immediately before submission."""
        latest = self._feedback(db, task_id)
        return (latest.id if latest else None) == assessment.get("feedback_id")

    def respond(
        self, task_id, assessment_id, snapshot_hash, action, text_value="",
        *, current_snapshot=None, text=None,
    ):
        # `text` is accepted for a natural JSON-to-keyword call at the API edge.
        message = text if text is not None else text_value
        if action not in {"answer", "approve", "hold"} or not isinstance(message, str) or len(message) > 8000:
            raise AppError("invalid_review_feedback", "人工反馈格式无效。", 422)
        if action == "answer" and not message.strip():
            raise AppError("invalid_review_feedback", "补充依据不能为空。", 422)
        if current_snapshot != snapshot_hash:
            raise AppError("approval_snapshot_changed", "审批内容已变化，请读取最新内容后处理。", 409)
        with self.sessions.begin() as db:
            review = db.scalar(
                select(ApprovalReview).where(ApprovalReview.task_id == task_id).with_for_update()
            )
            account = db.get(Account, 1)
            row = db.get(ModelAssessment, assessment_id)
            feedback_current = (
                row is not None
                and self.feedback_matches(db, task_id, {"feedback_id": row.feedback_id})
            )
            if (
                not review or not row
                or not feedback_current
                or review.assessment_id != assessment_id or review.snapshot_hash != snapshot_hash
                or row.task_id != task_id or row.snapshot_hash != snapshot_hash
                or not account or row.owner_open_id != account.open_id
                or review.owner_open_id != account.open_id or row.instance_code != review.instance_code
                or review.submitted_at is not None
                or review.execution_status in {"submitting", "unknown", "approved", "not_pending"}
            ):
                raise AppError("approval_feedback_stale", "该评估已失效或已经提交，不能重复操作。", 409)
            feedback = ApprovalFeedback(
                id=str(uuid4()), task_id=task_id, assessment_id=assessment_id,
                snapshot_hash=snapshot_hash, owner_open_id=account.open_id,
                action=action, text=message.strip(),
            )
            db.add(feedback)
            review.reviewed_at = utcnow() - timedelta(seconds=61)
            review.decision, review.execution_status = "needs_review", "awaiting_review"
            review.model_review = {
                **(review.model_review or {}), "eligible": False, "feedback_pending": True,
                "held": action == "hold",
            }
            db.flush()
            return {
                "feedback_id": feedback.id, "task_id": task_id, "action": action,
                "assessment_id": assessment_id, "snapshot_hash": snapshot_hash,
                "created_at": feedback.created_at.isoformat(),
            }
