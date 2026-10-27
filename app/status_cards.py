"""Compact display-only Feishu cards, separate from historical notice revisions."""

import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

FORMAT_VERSION = 1
# Public Feishu approval app ID from the documented approval AppLink protocol.
APPROVAL_APP_ID = "cli_9cb844403dbb9108"


def _display(value, limit):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"https?://[^\s<>]+", "", value)
    value = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def brief_reason(*candidates):
    """At most one useful sentence; omit navigation and implementation footers."""
    for candidate in candidates:
        values = candidate if isinstance(candidate, (list, tuple)) else [candidate]
        for value in values:
            text = _display(value, 300)
            if not text or any(word in text for word in ("工作台", "下一步：", "处理边界：", "处理范围：")):
                continue
            sentence = re.split(r"(?<=[。！？!?])|[；;]", text, maxsplit=1)[0].strip()
            if sentence:
                return _display(sentence, 100)
    return ""


def chat_button(chat_id):
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]{1,124}", chat_id):
        return None
    return {
        "label": "查看原会话",
        "url": "https://applink.feishu.cn/client/chat/open?" + urlencode({"openChatId": chat_id}),
    }


def approval_button(instance_code):
    if not isinstance(instance_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance_code):
        return None
    # The official protocol accepts both an instance ID and an instance code.
    mobile = "https://applink.feishu.cn/client/mini_program/open?" + urlencode({
        "appId": APPROVAL_APP_ID,
        "path": "pages/detail/index?" + urlencode({"instanceId": instance_code}),
    })
    desktop = "https://applink.feishu.cn/client/mini_program/open?" + urlencode({
        "appId": APPROVAL_APP_ID, "mode": "appCenter",
        "path": "pc/pages/in-process/index?" + urlencode({"instanceId": instance_code}),
    })
    return {
        "label": "查看申请", "url": mobile, "pc_url": desktop,
    }


def dms_button(source_url, chat_id):
    if isinstance(source_url, str) and len(source_url) <= 8000:
        try:
            parsed = urlsplit(source_url)
            if (
                parsed.scheme == "https" and parsed.hostname == "dms.aliyun.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and not re.search(r"[\x00-\x20\x7f]", source_url)
            ):
                return {"label": "查看申请", "url": source_url}
        except ValueError:
            pass
    return chat_button(chat_id)


def dms_title(source, old_title=""):
    kind, identifier = source.get("task_kind"), source.get("task_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,32}", identifier):
        key = source.get("task_key") or old_title
        match = re.search(r"DMS-([^\r\n]{1,100})-([0-9]{1,32})$", key) if isinstance(key, str) else None
        kind, identifier = match.groups() if match else (None, None)
    kind = _display(kind, 40)
    return f"DMS {kind or '审批'} #{identifier}" if identifier else f"DMS {kind or '审批'}"


def attach_card(notice, *, title, status, detail="", button=None):
    if notice is None:
        return None
    facts = {"title": _display(title, 80), "status": _display(status, 80)}
    if detail := brief_reason(detail):
        facts["detail"] = detail
    if button:
        facts["button"] = button
    # The existing revision intentionally fingerprints only the legacy semantic
    # projection. A layout change must not re-announce historical sources.
    return {**notice, "card": facts}


def _facts(payload):
    current = payload.get("card")
    if isinstance(current, dict) and current.get("title") and current.get("status"):
        return current
    # Old queued payloads may predate card projection. A fresh observation will
    # supply richer facts; the fallback never infers approval from disappearance.
    kind, phase = payload.get("source_type"), payload.get("phase")
    title = _display(payload.get("title"), 80) or "事项提醒"
    title = re.sub(r"^(?:审批状态：|收到 @ 你的消息：|@ 消息状态：)", "", title)
    if kind == "dms_card":
        title = dms_title({}, title)
    statuses = {
        "received": "⏳ 正在核验", "assessing": "⏳ 正在核验",
        "needs_human": "🔔 待你处理", "approved": "✅ 已通过",
        "rejected": "❌ 已拒绝", "transferred": "↪️ 已转交", "completed": "ℹ️ 本人节点已结束",
        "submitting": "⏳ 正在提交，结果未确认", "submission_unknown": "⚠️ 提交结果未确认",
        "responded": "ℹ️ 已停止提醒", "acknowledged": "⏸ 已停止提醒",
        "held": "⏸ 已暂缓", "error": "⚠️ 核验受阻",
        "normal": "ℹ️ 无需电话提醒", "reminding": "⏳ 正在发送提醒",
        "phone_accepted": "📞 电话提醒已受理",
        "waiting": "🔔 等待处理",
    }
    facts = {"title": title, "status": statuses.get(phase, "⏳ 正在核验")}
    text = payload.get("text") if isinstance(payload.get("text"), str) else ""
    match = re.search(r"^原会话：https://applink\.feishu\.cn/client/chat/open\?openChatId=(oc_[A-Za-z0-9_-]+)$", text, re.MULTILINE)
    if match:
        facts["button"] = chat_button(match[1])
    return facts


def compact_text(payload):
    """Short audit text without actionable URL strings or historical footers."""
    facts = _facts(payload)
    lines = [_display(facts.get("title"), 80), _display(facts.get("status"), 80)]
    if detail := brief_reason(facts.get("detail")):
        lines.append(detail)
    return "\n".join(line for line in lines if line)


def _valid_url(value, label):
    if not isinstance(value, str) or len(value) > 8000 or re.search(r"[\x00-\x20\x7f]", value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return False
        if label == "查看申请" and parsed.hostname == "dms.aliyun.com":
            return True
        if parsed.hostname != "applink.feishu.cn":
            return False
        query = parse_qs(parsed.query)
        if label == "查看原会话" and parsed.path == "/client/chat/open":
            return bool(re.fullmatch(r"oc_[A-Za-z0-9_-]{1,124}", query.get("openChatId", [""])[0]))
        if label == "查看申请" and parsed.path == "/client/mini_program/open":
            return query.get("appId") == [APPROVAL_APP_ID] and bool(re.fullmatch(
                r"(?:pages/detail/index|pc/pages/in-process/index)\?instanceId=[A-Za-z0-9_-]{1,128}",
                query.get("path", [""])[0],
            ))
    except ValueError:
        return False
    return False


def status_card(payload):
    """Render a shared v1 card with plain text and URL-only buttons."""
    facts = _facts(payload)
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": _display(facts.get("status"), 80)}}]
    if detail := brief_reason(facts.get("detail")):
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": detail}})
    button = facts.get("button")
    if isinstance(button, dict) and _valid_url(button.get("url"), button.get("label")):
        links = {"url": button["url"]}
        if _valid_url(button.get("pc_url"), button["label"]):
            links = {"multi_url": {
                "url": button["url"], "pc_url": button["pc_url"],
                "ios_url": button["url"], "android_url": button["url"],
            }}
        elements.append({
            "tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": button["label"]},
                "type": "default", **links,
            }],
        })
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": _display(facts.get("title"), 80)}},
        "elements": elements,
    }


def card_fingerprint(payload):
    return hashlib.sha256(json.dumps(
        [FORMAT_VERSION, status_card(payload)], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
