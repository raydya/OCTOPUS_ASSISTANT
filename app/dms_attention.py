"""Read-only, allowlisted DMS cards for the shared reminder delivery.

Only the explicit two-button pending layout is actionable. This module never
opens DMS links, invokes card actions, or grants approval. A verified terminal
state permanently closes this task's reminder, even if an old card is replayed.
"""

import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select

from app.content import message_text
from app.errors import AppError
from app.models import Account, AttentionState, Chat, Message, MessageAttention, utcnow

TITLE = re.compile(r"^DMS-(?P<kind>[^\r\n]{1,100})-(?P<task>[0-9]{1,32})$")
TERMINAL = {"approved", "rejected", "cancelled"}
TERMINAL_BUTTONS = {
    "已同意": "approved", "Approved": "approved",
    "已拒绝": "rejected", "Rejected": "rejected",
    "已撤销": "cancelled",
}
MAX_TIMESTAMP = 253402300799999


def dms_attention_id(app_id, chat_id, task_key, owner):
    identity = json.dumps([app_id, chat_id, task_key, owner], ensure_ascii=False, separators=(",", ":"))
    return "dms_" + hashlib.sha256(identity.encode()).hexdigest()


def _problem(status, reason, error=None):
    return status, [reason], error


def _timestamp(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
        value = int(value)
    return value if type(value) is int and 0 < value <= MAX_TIMESTAMP else None


def _safe_url(value):
    if not isinstance(value, str) or not value or len(value) > 8000:
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == "dms.aliyun.com"
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def _button_labels(elements):
    """Read only button elements, never status words in explanatory text."""
    labels, count = [], 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 10 or count > 2000:
            raise ValueError("card_too_complex")
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            if value.get("tag") == "button":
                label = value.get("text")
                if not isinstance(label, str):
                    raise ValueError("button_text_unknown")
                label = label.strip()
                if label in {"同意", "拒绝"} and "disabled" in value and value["disabled"] is not False:
                    raise ValueError("pending_button_disabled")
                labels.append(label)
            # The verified GET-message shape is rows of elements. Do not scan
            # arbitrary nested metadata or embedded examples for action labels.
        else:
            raise ValueError("card_element_unknown")

    if not isinstance(elements, list):
        raise ValueError("card_elements_unknown")
    visit(elements)
    return labels


def _parse(raw):
    """Return a task identity with pending/terminal/unknown, or no identity."""
    if not isinstance(raw, dict) or not isinstance(raw.get("body"), dict):
        return None
    content = raw["body"].get("content")
    try:
        body = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("title"), str):
        return None
    title = body["title"].strip()
    match = TITLE.fullmatch(title)
    if not match:
        return None
    created = _timestamp(raw.get("create_time"))
    update_value = raw.get("update_time")
    updated = _timestamp(update_value)
    valid_time = bool(created) and (
        update_value is None or update_value in ("", "0")
        or (type(update_value) is int and update_value == 0) or updated is not None
    )
    card = {
        "message_id": raw.get("message_id"), "task_key": title,
        "task_id": match["task"], "task_kind": match["kind"],
        "create_time": created, "update_time": updated,
        "version": max(created or 0, updated or 0),
        "status": "unknown", "buttons": [], "error": "dms_card_unknown",
        "hash": "",
    }
    try:
        card["hash"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        if not valid_time or not isinstance(card["message_id"], str) or not card["message_id"]:
            card["error"] = "dms_card_identity_unknown"
            return card
        if raw.get("deleted"):
            card["error"] = "dms_card_deleted"
            return card
        links = body.get("card_link")
        if not isinstance(links, dict) or not _safe_url(links.get("url")):
            card["error"] = "dms_card_link_unverified"
            return card
        if any(
            links.get(key) not in (None, "") and not _safe_url(links[key])
            for key in ("android_url", "ios_url", "pc_url")
        ):
            card["error"] = "dms_card_link_unverified"
            return card
        buttons = _button_labels(body.get("elements"))
        card["buttons"] = buttons
        if len(buttons) == 2 and set(buttons) == {"同意", "拒绝"}:
            card["status"], card["error"] = "pending", None
        elif len(buttons) == 1 and buttons[0] in TERMINAL_BUTTONS:
            card["status"], card["error"] = TERMINAL_BUTTONS[buttons[0]], None
        return card
    except (ValueError, TypeError, RecursionError):
        return card


def _metadata(card, app_id, chat_id, old=None):
    return {
        **(old or {}),
        "source_message_id": card["message_id"], "sender_app_id": app_id, "chat_id": chat_id,
        "task_id": card["task_id"], "task_kind": card["task_kind"], "task_key": card["task_key"],
        "card_status": card["status"], "card_buttons": card["buttons"],
        "source_create_time": card["create_time"], "source_update_time": card["update_time"],
        "source_version": card["version"],
    }


def _presentation(card):
    pending = card["status"] == "pending"
    return {
        "source": "dms_card", "decision": "needs_action" if pending else "unknown",
        "summary": (
            f"{card['task_key']} 的卡片显示“同意 / 拒绝”，需要本人处理。"
            if pending else f"{card['task_key']} 的当前处理状态尚未可靠识别。"
        ),
        "reasons": ["只识别已核实的发送应用、私聊与明确按钮状态；不会自动审批。"],
        "questions": [], "card_status": card["status"],
    }


class DmsCardAttentionSource:
    def __init__(self, settings, sessions, feishu, *, clock=None):
        self.settings, self.sessions, self.feishu = settings, sessions, feishu
        self.clock = clock or utcnow
        self.app_id = getattr(settings, "dms_sender_app_id", "")
        self.chat_id = getattr(settings, "dms_chat_id", "")

    @property
    def configured(self):
        return bool(self.app_id and self.chat_id)

    def _origin(self, raw, account, chat):
        sender = raw.get("sender") if isinstance(raw, dict) else None
        return bool(
            self.configured and account and account.open_id and account.tenant_key
            and chat and chat.visible and chat.chat_id == self.chat_id and chat.chat_mode == "p2p"
            and isinstance(raw, dict) and raw.get("msg_type") == "interactive"
            and raw.get("chat_id") == self.chat_id and raw.get("chat_type") == "p2p"
            and isinstance(sender, dict) and sender.get("id") == self.app_id
            and sender.get("id_type") == "app_id" and sender.get("sender_type") == "app"
            and sender.get("tenant_key") == account.tenant_key
        )

    def _latest(self, db, account, chat, candidate):
        # The numeric task id is ASCII even when body.content JSON escapes its
        # Chinese title. Parse candidates again to reject substring collisions.
        messages = db.scalars(select(Message).where(
            Message.chat_id == self.chat_id, Message.msg_type == "interactive",
            Message.raw["body"]["content"].as_string().contains(candidate["task_id"], autoescape=True),
        )).all()
        cards = [candidate]
        for message in messages:
            if not self._origin(message.raw, account, chat):
                continue
            card = _parse(message.raw)
            if card and card["task_key"] == candidate["task_key"]:
                if card["message_id"] != message.message_id:
                    card["status"], card["error"] = "unknown", "dms_card_identity_unknown"
                cards.append(card)
        # A completed task cannot become a new reminder because of a replayed
        # pending card. Reopened work needs a distinct DMS task identity.
        terminal = [card for card in cards if card["status"] in TERMINAL]
        if terminal:
            return max(terminal, key=lambda item: (item["version"], str(item["message_id"] or "")))
        uncertain = [card for card in cards if card["error"] == "dms_card_identity_unknown"]
        if uncertain:
            return uncertain[0]
        latest_version = max(card["version"] for card in cards)
        newest = [card for card in cards if card["version"] == latest_version]
        hashes = {card["hash"] for card in newest}
        if len(hashes) > 1:
            result = dict(max(newest, key=lambda item: str(item["message_id"] or "")))
            result["status"], result["error"] = "unknown", "dms_card_version_ambiguous"
            return result
        return max(newest, key=lambda item: str(item["message_id"] or ""))

    def _close(self, row, card, set_status=None):
        row.source_data = {
            **_metadata(card, self.app_id, self.chat_id, row.source_data),
            "terminal_observed": True,
        }
        row.source_hash = card["hash"] or row.source_hash
        row.model_result = {
            **(row.model_result or {}), "source": "dms_card",
            "decision": "resolved", "card_status": card["status"],
            "summary": f"{card['task_key']} 已显示终态，停止本任务提醒。",
        }
        if not row.acknowledged_at and set_status:
            set_status(row, "responded", ["DMS 卡片已显示同意、拒绝或撤销等终态，停止本任务提醒。"])

    def observe(self, db, state, chat, message, now, set_status):
        if not self.configured:
            return False
        account = db.get(Account, 1, populate_existing=True)
        if not account or account.open_id != state.owner_open_id or not self._origin(message.raw, account, chat):
            return False
        raw = message.raw
        if raw.get("message_id") != message.message_id or message.chat_id != self.chat_id:
            return True
        card = _parse(raw)
        if card is None:
            # A recalled or unparseable update must not leave an existing
            # pending record actionable. It cannot invent a new task identity.
            rows = db.scalars(select(MessageAttention).where(
                MessageAttention.source_type == "dms_card",
                MessageAttention.owner_open_id == account.open_id,
                MessageAttention.source_data["source_message_id"].as_string() == message.message_id,
            ).with_for_update()).all()
            for row in rows:
                if not row.acknowledged_at and not row.phone_submitted_at and not row.reminder_submitted_at:
                    set_status(row, "blocked", ["DMS 卡片已撤回或无法解析，未触发电话。"], "dms_card_unknown")
            return bool(rows) or raw.get("msg_type") == "interactive"
        identifier = dms_attention_id(self.app_id, self.chat_id, card["task_key"], account.open_id)
        row = db.get(MessageAttention, identifier, with_for_update=True)
        if row and (
            row.owner_open_id != account.open_id or row.source_type != "dms_card"
            or (row.source_data or {}).get("task_key") != card["task_key"]
        ):
            return True
        if row and (row.source_data or {}).get("terminal_observed"):
            return True
        if card["status"] in TERMINAL:
            if row:
                self._close(row, card, set_status)
            return True
        card = self._latest(db, account, chat, card)
        if card["status"] in TERMINAL:
            if row:
                self._close(row, card, set_status)
            return True
        if row and (row.acknowledged_at or row.reminder_submitted_at or row.phone_submitted_at):
            return True
        previous = (row.source_data or {}) if row else {}
        if row and card["version"] < (previous.get("source_version") or 0):
            return True
        if row and row.source_hash == card["hash"] and previous.get("card_status") == card["status"]:
            row.source_data = _metadata(card, self.app_id, self.chat_id, previous)
            if row.status == "source_changed":
                if card["status"] == "pending":
                    set_status(
                        row, "waiting", ["已核实更新后的原卡片来源，业务内容未变，沿用原提醒期限。"],
                        next_attempt=max(row.due_at, now),
                    )
                else:
                    set_status(
                        row, "blocked", ["DMS 卡片状态仍未可靠识别，未触发电话。"],
                        card["error"] or "dms_card_unknown", next_attempt=now + timedelta(seconds=60),
                    )
            return True
        if row is None:
            row = MessageAttention(
                message_id=identifier, source_type="dms_card", owner_open_id=account.open_id,
                chat_id=self.chat_id, assessment_cache={}, assessment_context=[], attempts=0,
            )
            db.add(row)
        row.source_data = _metadata(card, self.app_id, self.chat_id, previous)
        row.source_hash = card["hash"] or hashlib.sha256(card["task_key"].encode()).hexdigest()
        row.chat_name = f"DMS 待审批：{card['task_key']}"
        row.text = card["task_key"]
        row.model_result = _presentation(card)
        row.source_created_at, row.assessed_at = now, now
        row.due_at = now + timedelta(seconds=self.settings.attention_timeout_seconds)
        if card["status"] == "pending":
            set_status(
                row, "waiting", ["首次确认当前 DMS 卡片待本人处理，等待 15 分钟后再次核验。"],
                next_attempt=row.due_at,
            )
        else:
            set_status(
                row, "blocked", ["DMS 卡片按钮或状态尚未可靠识别，未触发电话。"],
                card["error"] or "dms_card_unknown", next_attempt=now + timedelta(seconds=60),
            )
        return True

    def _checked_local(self, db, row, *, allow_unknown=False):
        account = db.get(Account, 1, populate_existing=True)
        chat = db.get(Chat, self.chat_id, populate_existing=True) if self.configured else None
        data = row.source_data or {}
        if (
            row.source_type != "dms_card" or not self.configured
            or not account or account.open_id != row.owner_open_id
            or row.chat_id != self.chat_id or data.get("chat_id") != self.chat_id
            or data.get("sender_app_id") != self.app_id
        ):
            return None, _problem("blocked", "DMS 提醒来源或当前账号绑定无法核实。", "dms_source_unverified")
        if row.acknowledged_at or data.get("terminal_observed"):
            return None, _problem("responded", "本任务已停止提醒或已确认处理结束。")
        source_id = data.get("source_message_id")
        message = db.get(Message, source_id, populate_existing=True) if isinstance(source_id, str) else None
        if not message or message.chat_id != self.chat_id or not self._origin(message.raw, account, chat):
            return None, _problem("blocked", "原 DMS 卡片不可用或发送身份无法确认。", "dms_source_unverified")
        card = _parse(message.raw)
        if not card or card["message_id"] != source_id or card["task_key"] != data.get("task_key"):
            return None, _problem("blocked", "原 DMS 卡片的任务身份无法核实。", "dms_card_unknown")
        latest = self._latest(db, account, chat, card)
        if latest["status"] in TERMINAL:
            self._close(row, latest)
            return None, _problem("responded", "DMS 卡片已显示终态，停止本任务提醒。")
        if latest["message_id"] != source_id:
            return None, _problem("source_changed", "DMS 卡片来源已更新，等待同步后重新核验。")
        if latest["status"] != "pending" and not allow_unknown:
            return None, _problem("blocked", "无法确认 DMS 卡片仍是待审批状态。", latest["error"] or "dms_card_unknown")
        if latest["status"] == "pending" and latest["hash"] != row.source_hash:
            return None, _problem("source_changed", "DMS 卡片或业务内容已更新，等待同步后重新核验。")
        return (account, chat, latest), (None, [], None)

    def local_check(self, db, row):
        return self._checked_local(db, row)[1]

    def _cache_changed_source(self, row, raw, fresh):
        """Make a verified update discoverable even outside the chat sync window."""
        with self.sessions.begin() as db:
            current = db.get(MessageAttention, row.message_id, with_for_update=True)
            state = db.get(AttentionState, 1, with_for_update=True)
            owner = db.get(Account, 1, populate_existing=True)
            chat = db.get(Chat, self.chat_id, populate_existing=True)
            if (
                not current or not state or not owner
                or current.owner_open_id != owner.open_id or state.owner_open_id != owner.open_id
                or not self._origin(raw, owner, chat)
            ):
                return _problem("blocked", "DMS 卡片更新时账号或来源无法核实。", "dms_source_unverified")
            data = current.source_data or {}
            if current.acknowledged_at or data.get("terminal_observed"):
                return _problem("responded", "本任务已停止提醒或已确认处理结束。")
            if data.get("source_message_id") != fresh["message_id"] or data.get("task_key") != fresh["task_key"]:
                return _problem("source_changed", "DMS 卡片来源已更新，等待下一轮核验。")
            message = db.get(Message, fresh["message_id"], with_for_update=True, populate_existing=True)
            cached = _parse(message.raw) if message and self._origin(message.raw, owner, chat) else None
            if (
                not cached or cached["message_id"] != fresh["message_id"]
                or cached["task_key"] != fresh["task_key"]
            ):
                return _problem("blocked", "无法核实待更新的原 DMS 卡片。", "dms_source_unverified")
            if cached["status"] in TERMINAL:
                self._close(current, cached)
                return _problem("responded", "本地 DMS 卡片已显示终态，停止本任务提醒。")
            if fresh["version"] < cached["version"]:
                return _problem("blocked", "DMS 实时返回的卡片版本落后，等待再次核验。", "dms_card_stale")
            if cached["status"] != "pending":
                # An outer flag (for example deleted) may change without the
                # card body hash changing. Preserve the observed unknown state
                # so discovery still grants a full grace period after recovery.
                current.source_data = {**data, "card_status": cached["status"]}
            message.raw = raw
            message.text = message_text(raw)
            message.deleted = False
            message.create_time = fresh["create_time"]
            # Discovery uses a (timestamp, message_id) cursor. Advance beyond
            # its timestamp even when a test/clock produces the same instant.
            message.synced_at = max(
                self.clock(), message.synced_at, state.discovery_at,
            ) + timedelta(microseconds=1)
        return _problem("source_changed", "DMS 最新业务内容已更新，下一轮重新计满 15 分钟。")

    def fresh_check(self, row):
        with self.sessions.begin() as db:
            current = db.get(MessageAttention, row.message_id, with_for_update=True)
            if not current:
                return _problem("blocked", "DMS 提醒记录已不可用。", "dms_source_unverified")
            # Unknown button layouts can later become actionable, including on
            # cards older than the normal chat sync window. Identity is still
            # strict; only this read path may continue past an unknown state.
            checked, problem = self._checked_local(db, current, allow_unknown=True)
            if problem[0]:
                return problem
            account, chat, card = checked
            source_id, owner = card["message_id"], current.owner_open_id
            needs_grace = (
                card["status"] != "pending"
                or (current.source_data or {}).get("card_status") != "pending"
            )
        try:
            raw = self.feishu.message(source_id)
            if (
                not self._origin(raw, account, chat) or raw.get("message_id") != source_id
            ):
                return _problem("blocked", "DMS 卡片的最新发送身份无法核实。", "dms_source_unverified")
            fresh = _parse(raw)
            if not fresh or fresh["task_key"] != card["task_key"]:
                return _problem("blocked", "DMS 最新卡片的任务身份无法核实。", "dms_card_unknown")
            if fresh["status"] in TERMINAL:
                with self.sessions.begin() as db:
                    current = db.get(MessageAttention, row.message_id, with_for_update=True)
                    if current and current.owner_open_id == owner:
                        self._close(current, fresh)
                return _problem("responded", "DMS 原卡片实时显示已处理或撤销，停止本任务提醒。")
            if fresh["status"] != "pending":
                return _problem("blocked", "DMS 最新卡片状态不明确，未触发电话。", fresh["error"] or "dms_card_unknown")
            if fresh["version"] < card["version"]:
                return _problem("blocked", "DMS 实时返回的卡片版本落后，等待再次核验。", "dms_card_stale")
            if needs_grace or fresh["hash"] != row.source_hash:
                return self._cache_changed_source(row, raw, fresh)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "dms_card_unavailable"
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", code):
                code = "dms_card_unavailable"
            return _problem("blocked", "未能实时核验 DMS 卡片，未触发电话。", code)
        with self.sessions.begin() as db:
            current = db.get(MessageAttention, row.message_id, with_for_update=True)
            if not current:
                return _problem("blocked", "DMS 提醒记录已不可用。", "dms_source_unverified")
            return self.local_check(db, current)

    def reminder_text(self, row):
        task = (row.source_data or {}).get("task_key") or row.text
        return (
            f"DMS 待审批提醒：{task}\n"
            f"确认待处理后已等待 {self.settings.attention_timeout_seconds // 60} 分钟，"
            "原卡片仍明确显示“同意 / 拒绝”。\n"
            "这里只提醒，不会自动同意、拒绝或操作 DMS。请打开原卡片确认并处理，"
            "或在工作台停止本次提醒。\n"
            "打开原会话：https://applink.feishu.cn/client/chat/open?"
            f"{urlencode({'openChatId': self.chat_id})}"
        )
