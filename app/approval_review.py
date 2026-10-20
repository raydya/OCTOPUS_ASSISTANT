"""Conservative local policy evaluation and durable, single-attempt approval.

Operator-owned rules and bounded model decisions authorize execution. Form text
is data and never creates rules. A committed submission marker is never cleared
or replayed, even after a timeout or crash; later ticks only read task status.
"""

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy import case, or_, select, text

from app.errors import AppError
from app.model_review import business_snapshot
from app.models import Account, ApprovalReview, ApprovalTask, SyncState, utcnow

WRITE_SCOPE = "approval:task:write"
REVIEW_INTERVAL = timedelta(seconds=60)
SNAPSHOT_MAX_AGE = timedelta(minutes=10)
LOCK_ID = 81024004
FORM_TYPES = {
    "input", "textarea", "number", "amount", "date", "dateInterval",
    "radioV2", "checkboxV2", "contactV2", "departmentV2", "telephone", "email", "address",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(value):
    return json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")),
    )


def _string(value, maximum=128):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _load_policy(settings):
    path = getattr(settings, "approval_policy_path", None)
    if not path:
        return {"version": "unconfigured", "rules": []}, None
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError
        policy = _decode(raw)
        if (
            not isinstance(policy, dict)
            or set(policy) != {"version", "rules"}
            or not _string(policy["version"])
            or not isinstance(policy["rules"], list)
            or len(policy["rules"]) > 100
        ):
            raise ValueError
        ids = set()
        for rule in policy["rules"]:
            required = {"id", "definition_code", "definition_name", "fields", "rationale"}
            if (
                not isinstance(rule, dict)
                or not required <= set(rule)
                or set(rule) - required - {"task_ids", "instance_codes", "form_sha256"}
                or not _string(rule["id"])
                or rule["id"] in ids
                or not _string(rule["definition_code"])
                or not _string(rule["definition_name"], 256)
                or not _string(rule["rationale"], 2000)
                or not isinstance(rule["fields"], dict)
                or not 1 <= len(rule["fields"]) <= 200
            ):
                raise ValueError
            ids.add(rule["id"])
            if "form_sha256" in rule and (
                not isinstance(rule["form_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", rule["form_sha256"])
            ):
                raise ValueError
            for binding in ("task_ids", "instance_codes"):
                if binding in rule and (
                    not isinstance(rule[binding], list)
                    or not 1 <= len(rule[binding]) <= 100
                    or any(not _string(value) for value in rule[binding])
                    or len(set(rule[binding])) != len(rule[binding])
                ):
                    raise ValueError
            for field_id, field in rule["fields"].items():
                if (
                    not _string(field_id)
                    or not isinstance(field, dict)
                    or set(field) != {"name", "type", "values"}
                    or not _string(field["name"], 256)
                    or field["type"] not in FORM_TYPES
                    or not isinstance(field["values"], list)
                    or not 1 <= len(field["values"]) <= 100
                ):
                    raise ValueError
        return policy, None
    except (OSError, ValueError, TypeError, RecursionError):
        return {"version": "invalid", "rules": []}, "invalid_approval_policy"


def policy_summary(settings):
    policy, error = _load_policy(settings)
    return {"version": policy["version"], "rule_count": len(policy["rules"]), "error_code": error}


def _form_fields(detail):
    form = detail.get("form")
    if isinstance(form, str):
        form = _decode(form)
    if not isinstance(form, list) or not form:
        raise ValueError
    result = {}
    seen = set()
    for field in form:
        if not isinstance(field, dict) or not _string(field.get("id")):
            raise ValueError
        if set(field) - {"id", "name", "type", "value", "ext", "option"}:
            raise ValueError
        if field["id"] in seen:
            raise ValueError
        seen.add(field["id"])
        if field.get("type") == "text":
            # Official readonly explanatory control; never read as instructions.
            continue
        if (
            field.get("type") not in FORM_TYPES
            or not _string(field.get("name"), 256)
            or "value" not in field
        ):
            raise ValueError
        result[field["id"]] = {
            "name": field["name"], "type": field["type"], "value": field["value"],
        }
    if not result:
        raise ValueError
    return result


def form_fingerprint(form):
    """Digest the full form, including readonly text and control metadata."""
    parsed = _decode(form) if isinstance(form, str) else form
    if not isinstance(parsed, list):
        raise ValueError("invalid form")
    return hashlib.sha256(_canonical(parsed).encode()).hexdigest()


def evaluate_policy(policy, detail, *, task_id=None):
    """Return (decision, reasons, rule_id); no account state or side effects."""
    name = detail.get("definition_name")
    if not isinstance(name, str):
        name = ""
    business_reasons = {
        "工程平台-应用申请": "需核验应用与代码仓库归属、负责人及访问路由是否冲突。",
        "工程平台-应用变更": "需核验变更影响、迁移及依赖切换证据，确认回退方案。",
    }
    try:
        fields = _form_fields(detail)
        fingerprint = form_fingerprint(detail.get("form"))
        form = _decode(detail["form"]) if isinstance(detail["form"], str) else detail["form"]
    except (ValueError, TypeError, RecursionError):
        return "needs_review", ["审批表单格式不完整或含暂不支持的控件，需人工核验。"], None
    matches = []
    for rule in policy["rules"]:
        if (
            rule["definition_code"] != detail.get("definition_code")
            or rule["definition_name"] != name
            or ("task_ids" in rule and task_id not in rule["task_ids"])
            or (
                "instance_codes" in rule
                and detail.get("instance_code") not in rule["instance_codes"]
            )
            or ("form_sha256" in rule and rule["form_sha256"] != fingerprint)
        ):
            continue
        if "form_sha256" not in rule and any(
            field.get(key) not in (None, "", [], {})
            for field in form for key in ("ext", "option")
        ):
            # Extra control metadata can change meaning without changing labels
            # or values. Exact instance rules bind the whole form to its digest.
            continue
        if set(rule["fields"]) != set(fields):
            continue
        if all(
            allowed["type"] == fields[field_id]["type"]
            and allowed["name"] == fields[field_id]["name"]
            and _canonical(fields[field_id]["value"]) in {
                _canonical(value) for value in allowed["values"]
            }
            for field_id, allowed in rule["fields"].items()
        ):
            matches.append(rule)
    if len(matches) == 1:
        rule = matches[0]
        return "eligible", [f"完整匹配规则 {rule['id']}：{rule['rationale']}"], rule["id"]
    reasons = [business_reasons[name]] if name in business_reasons else []
    reasons.append(
        "匹配了多个规则，需消除政策歧义。"
        if matches else "没有覆盖该审批定义及全部表单字段的已核验自动通过规则。"
    )
    return "needs_review", reasons, None


def _current_task(detail, task_id):
    tasks = detail.get("tasks")
    if not isinstance(tasks, list):
        return None
    matches = [task for task in tasks if isinstance(task, dict) and task.get("id") == task_id]
    return matches[0] if len(matches) == 1 else None


def _eligibility_errors(task, detail, task_id, instance_code, open_id):
    errors = []
    if (
        task.get("task_id") != task_id
        or task.get("instance_code") != instance_code
        or detail.get("instance_code") != instance_code
        or not detail.get("definition_code")
        or task.get("definition_code") != detail.get("definition_code")
    ):
        errors.append("待办与审批详情的身份或定义不一致，需人工核验。")
    if task.get("user_id") != open_id:
        errors.append("该待办不属于当前绑定用户。")
    if task.get("support_api_operate") is not True:
        errors.append("当前任务未明确支持 API 审批。")
    if (
        str(task.get("status")) != "1"
        or str(task.get("topic")) != "1"
        or str(task.get("instance_status")) != "1"
        or detail.get("status") != "PENDING"
        or detail.get("reverted") is not False
    ):
        errors.append("审批已不处于可处理的待办状态，或撤销状态未确认。")
    current = _current_task(detail, task_id)
    if not current or current.get("user_id") != open_id or current.get("status") != "PENDING":
        errors.append("详情中未确认当前用户的待处理任务。")
    nodes = detail.get("current_nodes")
    active = [
        (node, approver)
        for node in nodes if isinstance(node, dict) and isinstance(node.get("approvers"), list)
        for approver in node["approvers"]
        if isinstance(approver, dict) and approver.get("task_id") == task_id
    ] if isinstance(nodes, list) else []
    if (
        len(active) != 1
        or active[0][1].get("user_id") != open_id
        or not current
        or active[0][0].get("node_id") != current.get("node_id")
        or not current.get("node_id")
    ):
        errors.append("当前节点未确认该用户及任务仍有审批资格。")
    return errors


def review_view(review):
    if review is None:
        return None
    return {
        key: getattr(review, key)
        for key in (
            "task_id", "instance_code", "decision", "execution_status",
            "policy_version", "rule_id", "fingerprint", "reasons", "error_code",
            "created_at", "reviewed_at", "submitted_at", "confirmed_at",
            "assessment_id", "snapshot_hash", "model_review",
        )
    }


class ApprovalReviewer:
    def __init__(self, settings, sessions, feishu, model_review=None):
        self.settings, self.sessions, self.feishu = settings, sessions, feishu
        self.model_review = model_review

    def tick(self):
        if not getattr(self.settings, "approval_automation_enabled", False):
            return None
        with self.sessions() as db:
            engine = db.get_bind()
        # A dedicated connection retains the session lock across the submission
        # marker's commit. Transaction locks cannot cover that crash boundary.
        with engine.connect() as guard:
            acquired = guard.scalar(text(f"SELECT pg_try_advisory_lock({LOCK_ID})"))
            guard.commit()
            if not acquired:
                return None
            try:
                return self._tick()
            finally:
                guard.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
                guard.commit()

    def _candidate(self, db):
        cutoff = utcnow() - REVIEW_INTERVAL
        submitted = db.scalar(
            select(ApprovalReview).where(
                ApprovalReview.submitted_at.is_not(None),
                ApprovalReview.execution_status.in_(["submitting", "unknown"]),
                ApprovalReview.reviewed_at <= cutoff,
            ).order_by(ApprovalReview.reviewed_at, ApprovalReview.task_id).limit(1)
        )
        submitted_candidate = (
            (submitted.task_id, submitted.instance_code, True) if submitted else None
        )
        state = db.get(SyncState, "approvals:1")
        if (
            not state or state.has_more or not state.completed_at
            or state.completed_at < utcnow() - SNAPSHOT_MAX_AGE
        ):
            return submitted_candidate
        task = db.scalar(
            select(ApprovalTask).outerjoin(
                ApprovalReview, ApprovalReview.task_id == ApprovalTask.task_id
            ).where(
                ApprovalTask.topic == "1",
                ApprovalTask.visible.is_(True),
                ApprovalTask.generation == state.generation,
                ApprovalReview.submitted_at.is_(None),
                or_(ApprovalReview.task_id.is_(None), ApprovalReview.reviewed_at <= cutoff),
            ).order_by(
                case((ApprovalReview.task_id.is_(None), 0), else_=1),
                ApprovalReview.reviewed_at.asc().nullsfirst(),
                ApprovalTask.task_id,
            ).limit(1)
        )
        if not task:
            return submitted_candidate
        previous = db.get(ApprovalReview, task.task_id)
        # New tasks are examined promptly; thereafter age orders both ordinary
        # reviews and ambiguous submissions so neither group can starve another.
        if submitted and previous and submitted.reviewed_at <= previous.reviewed_at:
            return submitted_candidate
        return task.task_id, task.instance_code, False

    def _tick(self):
        with self.sessions() as db:
            account = db.get(Account, 1)
            candidate = self._candidate(db) if account else None
            if not candidate:
                return None
            open_id, scopes = account.open_id, set(account.scope.split())
        task_id, instance_code, submitted = candidate
        if submitted:
            return self._confirm(task_id, instance_code, open_id)
        policy, policy_error = _load_policy(self.settings)
        detail, task, read_error = {}, {}, None
        pending_found, more = False, False
        try:
            listing = self.feishu.tasks("1", None)
            rows = listing.get("tasks") if isinstance(listing, dict) else None
            if not isinstance(rows, list):
                raise AppError("invalid_approval_tasks", "待办返回格式无效。")
            matches = [
                row for row in rows if isinstance(row, dict) and row.get("task_id") == task_id
            ]
            if len(matches) > 1:
                raise AppError("invalid_approval_tasks", "待办标识重复。")
            pending_found = len(matches) == 1
            task = matches[0] if pending_found else {}
            more = listing.get("has_more") is not False
            detail = self.feishu.instance(instance_code)
            if not isinstance(detail, dict):
                raise AppError("invalid_approval_detail", "审批详情返回格式无效。")
        except AppError as exc:
            read_error = exc.code
        decision, reasons, rule_id = evaluate_policy(policy, detail, task_id=task_id)
        if policy_error:
            decision, rule_id = "needs_review", None
            reasons.append("审批政策文件无效，需修复政策后重新核验。")
        if read_error:
            decision, rule_id = "needs_review", None
            reasons.append("本次无法读取最新待办或详情，未提交审批。")
        elif not pending_found:
            decision, rule_id = ("needs_review" if more else "not_pending"), None
            reasons.append(
                "最新待办第一页未找到该任务，不能确认其审批资格。"
                if more else "最新完整待办列表中已无此任务，未提交审批。"
            )
        errors = []
        if not read_error and pending_found:
            errors = _eligibility_errors(task, detail, task_id, instance_code, open_id)
            if errors:
                decision, rule_id = "needs_review", None
                reasons.extend(errors)
        model_result = None
        if (
            self.model_review and decision == "needs_review" and not errors
            and not read_error and not policy_error and pending_found
        ):
            model_result = self.model_review.evaluate(task, detail, open_id)
            decision = "eligible" if model_result["eligible"] else "needs_review"
            reasons = [model_result["summary"], *model_result["reasons"]]
            rule_id = (
                f"{model_result['source']}:{model_result['assessment_id']}"
                if decision == "eligible" else None
            )
            if decision == "eligible":
                # A model request may take minutes. Re-read both the live queue
                # and full detail and bind them to the assessed business input.
                try:
                    latest = self.feishu.tasks("1", None)
                    latest_rows = latest.get("tasks") if isinstance(latest, dict) else None
                    matches = [
                        item for item in latest_rows
                        if isinstance(item, dict) and item.get("task_id") == task_id
                    ] if isinstance(latest_rows, list) else []
                    latest_detail = self.feishu.instance(instance_code)
                    if (
                        len(matches) != 1 or not isinstance(latest_detail, dict)
                        or _eligibility_errors(
                            matches[0], latest_detail, task_id, instance_code, open_id,
                        )
                        or business_snapshot(matches[0], latest_detail, open_id)
                        != model_result["snapshot_hash"]
                    ):
                        decision, rule_id = "needs_review", None
                        reasons.append("模型判断后审批内容或处理资格已变化，等待按最新内容重新判断。")
                        model_result = {**model_result, "eligible": False, "stale": True}
                except AppError as exc:
                    read_error = exc.code
                    decision, rule_id = "needs_review", None
                    reasons.append("模型判断后无法再次读取最新状态，未提交审批。")
                    model_result = {**model_result, "eligible": False, "stale": True}
        execution = "awaiting_review"
        error_code = read_error or policy_error or (model_result or {}).get("error_code")
        if decision == "not_pending":
            execution = "not_pending"
        elif decision == "eligible" and WRITE_SCOPE not in scopes:
            execution, error_code = "blocked_permission", "approval_write_scope_required"
            reasons.append("业务规则已匹配，但用户授权缺少 approval:task:write。")
        elif decision == "eligible":
            execution = "submitting"
        fingerprint = hashlib.sha256(_canonical({
            "policy": policy, "task": task, "detail": detail, "owner": open_id,
        }).encode()).hexdigest()
        with self.sessions.begin() as db:
            row = db.scalar(
                select(ApprovalReview).where(ApprovalReview.task_id == task_id).with_for_update()
            )
            if row and row.submitted_at:
                return review_view(row)
            # Fetch before adding an incomplete new model: ORM autoflush must
            # never insert an audit row before its required fields are assigned.
            current_account = db.get(Account, 1)
            feedback_current = (
                not model_result
                or self.model_review.feedback_matches(db, task_id, model_result)
            )
            if row is None:
                row = ApprovalReview(
                    task_id=task_id, instance_code=instance_code, owner_open_id=open_id,
                )
                db.add(row)
            # Account ownership cannot silently change between evaluation and write.
            if (
                not current_account or current_account.open_id != open_id
                or row.instance_code != instance_code or row.owner_open_id != open_id
            ):
                decision, execution, rule_id = "needs_review", "awaiting_review", None
                reasons.append("绑定用户或任务关联已变化，需人工核验。")
            if (
                model_result and execution == "submitting"
                and not feedback_current
            ):
                decision, execution, rule_id = "needs_review", "awaiting_review", None
                reasons.append("本人反馈在判断期间发生变化，等待重新核验。")
                model_result = {**model_result, "eligible": False, "stale": True}
            row.decision, row.execution_status = decision, execution
            row.policy_version, row.rule_id = policy["version"], rule_id
            row.fingerprint, row.reasons, row.error_code = fingerprint, reasons, error_code
            row.assessment_id = (model_result or {}).get("assessment_id")
            row.snapshot_hash = (model_result or {}).get("snapshot_hash")
            row.model_review = model_result
            row.reviewed_at = utcnow()
            if execution == "submitting":
                row.submitted_at = utcnow()
            db.flush()
            view = review_view(row)
        if execution != "submitting":
            return view
        try:
            self.feishu.approve_task(
                instance_code, task_id, "同意。已按授权核验当前申请内容。",
            )
        except Exception as exc:
            # Even a provider error is never automatically re-posted. Network
            # failures may have committed remotely; only reads can resolve them.
            code = exc.code if isinstance(exc, AppError) else "approval_submit_unknown"
            return self._mark_unknown(task_id, code)
        return self._confirm(task_id, instance_code, open_id)

    def _mark_unknown(self, task_id, code):
        with self.sessions.begin() as db:
            row = db.scalar(
                select(ApprovalReview).where(ApprovalReview.task_id == task_id).with_for_update()
            )
            if row.execution_status == "approved":
                return review_view(row)
            row.execution_status, row.error_code = "unknown", code
            row.reviewed_at = utcnow()
            reason = "已进入提交阶段，结果尚未确认；只回读状态，不自动重复提交。"
            if reason not in row.reasons:
                row.reasons = [*row.reasons, reason]
            db.flush()
            return review_view(row)

    def _confirm(self, task_id, instance_code, open_id):
        try:
            detail = self.feishu.instance(instance_code)
        except AppError as exc:
            return self._mark_unknown(task_id, exc.code)
        task = _current_task(detail, task_id) if isinstance(detail, dict) else None
        with self.sessions.begin() as db:
            row = db.scalar(
                select(ApprovalReview).where(ApprovalReview.task_id == task_id).with_for_update()
            )
            if row.execution_status == "approved":
                return review_view(row)
            confirmed = (
                isinstance(detail, dict) and detail.get("instance_code") == instance_code
                and row.instance_code == instance_code and row.owner_open_id == open_id
                and task and task.get("user_id") == open_id and task.get("status") == "APPROVED"
            )
            row.execution_status = "approved" if confirmed else "unknown"
            row.reviewed_at = utcnow()
            if confirmed:
                row.confirmed_at, row.error_code = utcnow(), None
                row.reasons = [*row.reasons, "回读详情已确认当前用户的该任务状态为 APPROVED。"]
            else:
                row.error_code = "approval_result_unconfirmed"
                reason = "回读未确认该任务已通过；保留待核实状态，不重复提交。"
                if reason not in row.reasons:
                    row.reasons = [*row.reasons, reason]
            db.flush()
            return review_view(row)
