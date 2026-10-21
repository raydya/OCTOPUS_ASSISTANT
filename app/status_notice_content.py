"""Project persisted facts into short owner-only status messages.

These functions do not read the database, call a model, or send messages. Model
recommendations are deliberately separate from confirmed approval execution.
Only selected plain-text fields enter a notice; tool output and model traces do
not. Revisions describe visible business information, not polling activity.
"""

import hashlib
import json
import re
from urllib.parse import urlencode, urlsplit


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _short(value, limit=300):
    if not isinstance(value, str):
        return ""
    # Each untrusted field remains one labelled line in this plain-text notice.
    value = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _items(value, limit=3):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for raw in value if (item := _short(raw, 220))))[:limit]


def _links(base_url, chat_id=""):
    lines = []
    if isinstance(chat_id, str) and chat_id:
        lines.append(
            "原会话：https://applink.feishu.cn/client/chat/open?"
            + urlencode({"openChatId": chat_id})
        )
    if isinstance(base_url, str) and base_url:
        try:
            parsed = urlsplit(base_url)
            if (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password
                and not re.search(r"[\x00-\x20\x7f]", base_url)
            ):
                lines.append(f"工作台：{base_url.rstrip('/')}")
        except ValueError:
            pass
    return lines


def _notice(source_type, source_id, phase, title, lines, *, terminal=False):
    if not isinstance(source_id, str) or not source_id:
        return None
    title = _short(title, 240)
    body = "\n".join([title, *(line for line in lines if line)])
    semantic = [source_type, source_id, phase, title, body, terminal]
    revision = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "source_type": source_type, "source_id": source_id, "phase": phase,
        "title": title, "text": body, "revision": revision, "terminal": terminal,
    }


def dms_receipt_revision(source_id):
    """One receipt per DMS task, independent of its changing card state."""
    return hashlib.sha256(json.dumps(["dms-received-v1", source_id]).encode()).hexdigest()


def dms_received_notice(payload, *, base_url="", chat_id=""):
    notice = _notice("dms_card", payload["source_id"], "received", payload["title"], [
        "当前状态：已收到，系统已登记这条 DMS 审批。",
        "下一步：核对原卡片状态，并汇报是否仍需你处理。",
        "处理范围：系统只核验状态和提醒，由你在原卡片完成审批。",
        *_links(base_url, chat_id),
    ])
    notice["revision"] = dms_receipt_revision(payload["source_id"])
    return notice


def _judgment_lines(model, reasons=()):
    lines = []
    if summary := _short(model.get("summary"), 500):
        lines.append(f"判断：{summary}")
    selected = _items(model.get("reasons")) or _items(reasons)
    if selected:
        lines.append("依据：" + "；".join(selected))
    if questions := _items(model.get("questions")):
        lines.append("需要你确认：" + "；".join(questions))
    return lines


def _review_matches(task, review):
    owner = _mapping(_get(task, "raw")).get("user_id")
    return bool(
        review and _get(review, "task_id") == _get(task, "task_id")
        and _get(review, "instance_code") == _get(task, "instance_code")
        and (not owner or _get(review, "owner_open_id") == owner)
    )


def _assessment_matches(task, assessment, current_snapshot_hash):
    owner = _mapping(_get(task, "raw")).get("user_id")
    snapshot = _get(assessment, "snapshot_hash")
    return bool(
        assessment and _get(assessment, "task_id") == _get(task, "task_id")
        and _get(assessment, "instance_code") == _get(task, "instance_code")
        and (not owner or _get(assessment, "owner_open_id") == owner)
        and isinstance(snapshot, str) and re.fullmatch(r"[a-f0-9]{64}", snapshot)
        and (
            current_snapshot_hash is None
            or bool(current_snapshot_hash)
            and _get(assessment, "snapshot_hash") == current_snapshot_hash
        )
    )


def _current_model(task, review, assessment, current_snapshot_hash):
    """Prefer an in-flight assessment only when it binds the current input."""
    cached = _mapping(_get(review, "model_review"))
    if cached.get("stale") and not (
        assessment and _get(assessment, "id") != _get(review, "assessment_id")
        and current_snapshot_hash
        and _get(assessment, "snapshot_hash") == current_snapshot_hash
    ):
        return {}
    assessment_is_current = _assessment_matches(task, assessment, current_snapshot_hash)
    if review:
        assessment_is_current = assessment_is_current and (
            _get(assessment, "owner_open_id") == _get(review, "owner_open_id")
            and (
                _get(assessment, "id") == _get(review, "assessment_id")
                and _get(assessment, "snapshot_hash") == _get(review, "snapshot_hash")
                or _get(assessment, "id") != _get(review, "assessment_id")
                and current_snapshot_hash
                and _get(assessment, "snapshot_hash") == current_snapshot_hash
            )
        )
    if assessment_is_current:
        return {
            **_mapping(_get(assessment, "result")),
            "status": _get(assessment, "status"),
        }
    if not review:
        return {}
    model = _mapping(_get(review, "model_review"))
    snapshot = _get(review, "snapshot_hash")
    if (
        not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot)
        or not _get(review, "assessment_id")
        or model.get("assessment_id") != _get(review, "assessment_id")
        or model.get("snapshot_hash") != snapshot
        or current_snapshot_hash is not None and snapshot != current_snapshot_hash
        or model.get("stale")
    ):
        return {}
    # An explicitly supplied conflicting assessment invalidates this cached view.
    if assessment and _get(assessment, "id") == _get(review, "assessment_id") and not (
        _assessment_matches(task, assessment, current_snapshot_hash)
        and _get(assessment, "snapshot_hash") == snapshot
        and _get(assessment, "owner_open_id") == _get(review, "owner_open_id")
    ):
        return {}
    return model


def approval_notice(
    task, review=None, assessment=None, *, base_url="", current_snapshot_hash=None,
):
    """Return the current task status, never treating an approve proposal as done.

    ``current_snapshot_hash=''`` explicitly means the caller could not verify
    current detail. ``None`` skips that additional check, while retaining the
    task/instance/owner and assessment bindings available in these arguments.
    """
    task_id = _get(task, "task_id")
    if not task_id or str(_get(task, "topic", "1")) != "1":
        return None
    title = "审批状态：" + (_short(_get(task, "title"), 180) or "未命名审批")
    matched = _review_matches(task, review)
    review = review if matched else None
    execution = _get(review, "execution_status")
    decision = _get(review, "decision")
    reasons = _get(review, "reasons", [])
    cached_model = _mapping(_get(review, "model_review"))
    model = _current_model(task, review, assessment, current_snapshot_hash)
    phase, terminal = "received", False
    status, next_step = "已收到，等待判断。", "后台将核验最新内容，再告知判断和处理结果。"
    show_judgment = False

    if execution == "approved" and _get(review, "confirmed_at"):
        phase, terminal = "approved", True
        status = "你的审批任务已通过，已回读飞书确认。"
        next_step = "本任务无需再审批；整个审批流程是否完成以飞书为准。"
        show_judgment = bool(model)
    elif execution in {"submitting", "unknown", "approved"} or _get(review, "submitted_at"):
        phase = "submitting" if execution == "submitting" else "submission_unknown"
        status = (
            "正在提交审批，尚未确认通过。" if phase == "submitting"
            else "审批提交结果尚未确认，不能视为已通过。"
        )
        next_step = "后台只回读结果，不重复提交；你可在飞书核对当前状态。"
        show_judgment = bool(model)
    elif (
        execution == "not_pending" or decision == "not_pending"
        or _get(task, "visible") is False or str(_get(task, "status", "1")) != "1"
    ):
        phase, terminal = "responded", True
        status = "该任务已不在当前待审批状态，无法仅据此认定审批通过。"
        next_step = "已停止本轮处理，最终结果请查看飞书审批。"
    elif cached_model.get("held") or model.get("held"):
        phase = "held"
        status = "你已暂缓这项审批。"
        next_step = "暂不自动审批或电话催办；需要继续时请在工作台补充信息。"
    elif cached_model.get("feedback_pending"):
        phase = "received"
        status = "已收到你的反馈，等待按最新信息重新判断。"
        next_step = "后台重新核验后会告知结论，当前反馈不代表审批已通过。"
    elif execution in {"failed", "blocked_permission"} or _get(review, "error_code"):
        phase = "error"
        status = (
            "审批执行权限不足，当前未提交。" if execution == "blocked_permission"
            else "当前核验或执行遇到问题，尚未确认审批通过。"
        )
        next_step = "请在工作台查看原因；后台会按现有策略继续核验。"
        show_judgment = True
    elif model.get("status") == "assessing":
        phase = "assessing"
        status = "已收到，模型正在判断，尚未提交审批。"
        next_step = "判断完成后会告知是否可自动处理、是否需要你介入。"
    elif model.get("status") == "failed":
        phase = "error"
        status = "模型暂时无法完成可靠判断，未自动审批。"
        next_step = "后台按现有策略重试，你也可在工作台或飞书处理。"
    elif model.get("status") == "complete":
        show_judgment = True
        if model.get("decision") == "approve" and model.get("eligible") is True:
            phase = "waiting"
            status = "当前判断建议通过，尚未确认审批执行。"
            next_step = "后台会核对最新内容和审批资格，提交后再确认结果。"
        else:
            phase = "needs_human"
            status = "当前判断需要你介入，未自动审批。"
            next_step = "请在工作台补充依据、确认通过或暂缓，也可在飞书直接处理。"
    elif cached_model or assessment:
        status = "已收到，当前评估尚未与最新审批内容核实一致。"
        next_step = "后台将重新核验，旧判断不作为当前处理结果。"
    elif decision == "eligible":
        phase, show_judgment = "waiting", True
        status = "当前规则核验允许自动处理，尚未确认审批通过。"
        next_step = "后台完成提交并回读确认后，会更新处理结果。"
    elif decision == "needs_review":
        phase, show_judgment = "needs_human", True
        status = "当前核验需要你介入，未自动审批。"
        next_step = "请查看核验依据，并在工作台补充信息或在飞书处理。"

    lines = [f"当前状态：{status}", f"审批任务：{task_id}"]
    if initiator := _short(_get(task, "initiator_name"), 100):
        lines.append(f"发起人：{initiator}")
    if show_judgment:
        lines.extend(_judgment_lines(model, reasons))
    lines.extend([f"下一步：{next_step}", *_links(base_url)])
    return _notice("approval", task_id, phase, title, lines, terminal=terminal)


def _message_received(message_id, chat_name, text, chat_id, base_url):
    return _notice(
        "message", message_id, "received", "收到 @ 你的消息：" + (_short(chat_name, 160) or "飞书会话"),
        [
            "当前状态：已收到，等待判断重要性与是否需要你响应。",
            f"消息摘要：{_short(text, 500) or '请打开原会话查看消息。'}",
            "下一步：判断完成后会私信告知；明确重要且需要响应的消息仍按原 15 分钟规则电话提醒。",
            *_links(base_url, chat_id),
        ],
    )


def mention_received_notice(message, chat, *, base_url=""):
    """Caller must first verify a visible chat and an explicit owner mention."""
    return _message_received(
        _get(message, "message_id"), _get(chat, "name"), _get(message, "text"),
        _get(message, "chat_id"), base_url,
    )


def attention_notice(row, *, base_url=""):
    """Project explicit mentions or DMS cards; native approvals use their review."""
    source_type = _get(row, "source_type", "message")
    if source_type not in {"message", "dms_card"}:
        return None
    dms = source_type == "dms_card"
    status = _get(row, "status")
    model = _mapping(_get(row, "model_result"))
    source = _mapping(_get(row, "source_data"))
    if not dms and status in {"queued", "assessing"} and not _get(row, "acknowledged_at"):
        return _message_received(
            _get(row, "message_id"), _get(row, "chat_name"), _get(row, "text"),
            _get(row, "chat_id"), base_url,
        )
    title = (
        "DMS 审批状态：" + (_short(source.get("task_key"), 180) or "待核实卡片")
        if dms else "@ 消息状态：" + (_short(_get(row, "chat_name"), 160) or "飞书会话")
    )
    phase, terminal = "received", False
    current = "已收到，等待核验卡片状态。" if dms else "已收到，等待判断。"
    next_step = "请在原卡片处理；后台只提醒，不自动同意或拒绝。" if dms else "后台完成判断后会更新状态。"
    show_judgment = False
    if _get(row, "acknowledged_at") or status == "acknowledged":
        phase, terminal = "acknowledged", True
        current = "你已停止本次提醒；这不代表 DMS 审批已通过。" if dms else "你已标记处理，已停止本次提醒。"
        next_step = "后续业务处理请以原会话或审批卡片为准。"
    elif status == "responded":
        phase, terminal = "responded", True
        labels = {"approved": "已同意", "rejected": "已拒绝", "cancelled": "已撤销"}
        card_status = labels.get(source.get("card_status"))
        current = (
            f"DMS 原卡片显示“{card_status}”，已停止本次提醒。" if dms and card_status
            else "DMS 已停止本次提醒，最终处理状态请查看原卡片。" if dms
            else "已观察到消息响应或停止提醒的条件，已停止本次电话提醒。"
        )
        next_step = "业务是否完成请以原会话或审批卡片为准。"
    elif status == "assessing":
        phase = "assessing"
        current = "正在核验 DMS 卡片状态。" if dms else "模型正在判断消息重要性与是否需要你响应。"
    elif status == "waiting":
        phase, show_judgment = "needs_human" if dms else "waiting", True
        current = "DMS 卡片仍待你审批，后台不会代批。" if dms else "判断为重要且需要你响应，正在等待你的处理。"
        next_step = (
            "请在原卡片处理；明确待审批满 15 分钟仍未处理时，按现有规则电话提醒。" if dms
            else "请在原会话响应；满 15 分钟仍未响应时，按现有规则电话提醒。"
        )
    elif status == "normal":
        phase, show_judgment = "normal", not dms
        current = (
            "当前未安排 DMS 电话提醒，不能据此判断审批已完成。" if dms
            else "判断为普通消息，仍需要你回复；不安排电话提醒。"
            if model.get("requires_response") is True
            else "当前判断无需你继续响应，不安排电话提醒。"
            if model.get("requires_response") is False
            else "当前判断无需电话提醒。"
        )
        next_step = (
            "DMS 处理状态请以原卡片为准。" if dms
            else "你可按需查看原会话；无需电话提醒不代表业务已经处理完成。"
        )
    elif status in {"model_error", "blocked", "blocked_permission", "source_unavailable"}:
        phase = "error"
        current = "当前核验或提醒受阻，尚不能确认可电话提醒。"
        next_step = "请查看原会话或工作台；后台按现有策略继续核验。"
    elif status == "source_changed":
        current = "原内容已变化，等待重新核验。"
        next_step = "重新判断后会更新状态，当前不沿用旧判断催办。"
    elif status == "skipped_restart":
        phase = "waiting"
        current = "服务恢复时已超过提醒期限，本次未补打电话。"
        next_step = "请查看原会话，或在工作台标记处理。"
    elif status in {"reminder_sending", "calling"}:
        phase = "reminding"
        current = "正在发送超时提醒。" if status == "reminder_sending" else "正在提交电话提醒请求。"
        next_step = "请查看原事项；请求提交不代表审批已处理或电话已接听。"
    elif status in {"reminder_unknown", "phone_unknown"}:
        phase = "submission_unknown"
        current = "超时提醒或电话请求的提交结果尚未确认。"
        next_step = "后台不会重复提交结果不明的请求，请直接查看原事项。"
    elif status == "phone_accepted":
        phase = "phone_accepted"
        current = "飞书已受理电话提醒请求；尚不能确认你已接听。"
        next_step = "请查看并处理原事项，同一任务不会重复电话催办。"

    lines = [f"当前状态：{current}"]
    if not dms:
        lines.append(f"消息摘要：{_short(_get(row, 'text'), 500) or '请打开原会话查看消息。'}")
    if show_judgment:
        lines.extend(_judgment_lines(model, _get(row, "reasons", [])))
    elif phase in {"error", "responded"}:
        if reasons := _items(_get(row, "reasons", []), 2):
            lines.append("核验说明：" + "；".join(reasons))
    if dms:
        lines.append("处理边界：仅提醒，不自动审批 DMS。")
    lines.extend([f"下一步：{next_step}", *_links(base_url, _get(row, "chat_id"))])
    return _notice(source_type, _get(row, "message_id"), phase, title, lines, terminal=terminal)
