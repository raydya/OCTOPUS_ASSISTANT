"""Escalate important mentions and approvals awaiting the owner's response.

Read state is deliberately tri-state: missing data never means unread. Every
outbound request has a durable marker, and ambiguous calls are never replayed.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy import func, or_, select, text, tuple_

from app.approval_attention import ApprovalAttentionSource
from app.content import message_text, parse_json
from app.dms_attention import DmsCardAttentionSource
from app.errors import AppError
from app.models import (
    Account,
    ApprovalReview,
    AttentionState,
    Chat,
    Message,
    MessageAttention,
    utcnow,
)

LOCK_ID = 81024005
MODEL = "gpt-6-sol"
EFFORT = "xhigh"
PROMPT_VERSION = "attention-v1"
MAX_ATTEMPTS = 3
TAIL_PAGE_LIMIT = 10
ACTIVE = {"queued", "assessing", "model_error", "waiting", "blocked", "blocked_permission"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(raw):
    try:
        value = int(raw.get("create_time", ""))
        return datetime.fromtimestamp(value / 1000, UTC) if value > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _sender_is(raw, owner):
    sender = raw.get("sender") or {}
    return (
        isinstance(sender, dict)
        and sender.get("sender_type") == "user"
        and sender.get("id_type") == "open_id"
        and sender.get("id") == owner
    )


def is_personal_mention(raw, owner, app_id=None):
    if not isinstance(raw, dict) or raw.get("deleted") or not _timestamp(raw):
        return False
    sender = raw.get("sender") or {}
    if (
        not isinstance(sender, dict)
        or (sender.get("sender_type"), sender.get("id_type")) not in {
            ("user", "open_id"), ("app", "app_id"),
        }
        or not sender.get("id")
        or (sender.get("id_type") == "open_id" and sender.get("id") == owner)
        or (app_id and sender.get("id_type") == "app_id" and sender.get("id") == app_id)
    ):
        return False
    mentions = raw.get("mentions")
    return isinstance(mentions, list) and any(
        isinstance(mention, dict)
        and mention.get("id_type") == "open_id"
        and mention.get("id") == owner
        for mention in mentions
    )


def source_hash(raw):
    """Ignore volatile counters and sender names; bind every message body field."""
    sender = raw.get("sender") or {}
    data = {
        key: raw.get(key) for key in (
            "message_id", "chat_id", "thread_id", "root_id", "parent_id", "msg_type",
        )
    }
    data.update({
        "sender": {key: sender.get(key) for key in ("id", "id_type", "sender_type")},
        "create_time": int(raw.get("create_time", 0)),
        "deleted": bool(raw.get("deleted")),
        "body": parse_json((raw.get("body") or {}).get("content", "")),
        "mentions": raw.get("mentions") or [],
    })
    return hashlib.sha256(_json(data).encode()).hexdigest()


def _model_message(raw):
    return {**raw, "text": message_text(raw)}


def _context_hash(context):
    return hashlib.sha256(_json([source_hash(item) for item in context]).encode()).hexdigest()


def _verified_result(result, payload):
    if (
        not isinstance(result, dict)
        or result.get("importance") not in {"high", "normal"}
        or type(result.get("requires_response")) is not bool
        or result.get("model") != MODEL
        or result.get("reasoning_effort") != EFFORT
        or not isinstance(result.get("summary"), str)
        or not 1 <= len(result["summary"]) <= 4000
        or not isinstance(result.get("reasons"), list)
        or len(result["reasons"]) > 30
        or any(not isinstance(item, str) or not item.strip() for item in result["reasons"])
        or not isinstance(result.get("evidence"), list)
        or len(result["evidence"]) > 30
    ):
        raise AppError("attention_model_invalid", "重要性判断格式无效。")
    for item in result["evidence"]:
        if (
            not isinstance(item, dict) or set(item) != {"source", "quote"}
            or item["source"] not in {"message", "context"}
            or not isinstance(item["quote"], str) or not 1 <= len(item["quote"]) <= 2000
            or not any(item["quote"] in representation for representation in (
                _json(payload[item["source"]]),
                json.dumps(payload[item["source"]], ensure_ascii=False),
                str(payload[item["source"]]),
            ))
        ):
            raise AppError("attention_evidence_invalid", "模型引用的消息依据无法验证。")
    if (
        result["importance"] == "high" and result["requires_response"]
        and not any(item["source"] == "message" for item in result["evidence"])
    ):
        raise AppError("attention_evidence_missing", "缺少原消息的重要性依据。")
    return {
        key: result[key] for key in (
            "importance", "requires_response", "summary", "reasons", "evidence",
            "model", "reasoning_effort",
        )
    } | {
        "usage": {
            key: value for key, value in (result.get("usage") or {}).items()
            if type(value) is int and value >= 0
        } if isinstance(result.get("usage"), dict) else {},
        "prompt_version": PROMPT_VERSION,
    }


def attention_view(row):
    result = {}
    for field in (
        "message_id", "chat_id", "chat_name", "text", "source_created_at", "due_at",
        "status", "model_result", "reasons", "error_code", "assessed_at",
        "reminder_message_id", "reminder_submitted_at", "phone_submitted_at",
        "phone_accepted_at", "acknowledged_at", "updated_at", "next_attempt_at",
        "source_type", "approval_task_id", "approval_instance_code", "approval_assessment_id",
        "source_data",
    ):
        value = getattr(row, field)
        result[field] = value.isoformat() if isinstance(value, datetime) else value
    result["can_acknowledge"] = row.acknowledged_at is None
    result["acknowledged"] = row.acknowledged_at is not None
    return result


view = attention_view


def _safe_error(exc):
    code = exc.code if isinstance(exc, AppError) else "attention_unavailable"
    return code if re.fullmatch(r"[a-zA-Z0-9_:-]{1,64}", code) else "attention_unavailable"


class AttentionService:
    def __init__(self, settings, sessions, feishu, provider):
        self.settings, self.sessions, self.feishu, self.provider = (
            settings, sessions, feishu, provider
        )
        self.started_at = utcnow()
        self.approvals = ApprovalAttentionSource(settings, sessions, feishu)
        self.dms = DmsCardAttentionSource(settings, sessions, feishu, clock=lambda: utcnow())
        self._dms_history_pending = self.dms.configured

    def tick(self):
        if not getattr(self.settings, "attention_enabled", False):
            return None
        with self.sessions() as db:
            engine = db.get_bind()
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

    def _tick(self):
        self._discover_dms_history()
        with self.sessions.begin() as db:
            owner = db.get(Account, 1)
            if not owner:
                return None
            state = db.get(AttentionState, 1)
            if state is None:
                now = utcnow()
                db.add(AttentionState(
                    id=1, owner_open_id=owner.open_id, activated_at=now,
                    discovery_at=now, discovery_message_id="",
                ))
                return None
            if state.owner_open_id != owner.open_id:
                raise AppError("attention_owner_changed", "提醒绑定的账号不一致。", 409)
            interrupted = db.scalars(select(MessageAttention).where(
                MessageAttention.owner_open_id == owner.open_id,
                MessageAttention.status.in_(["reminder_sending", "calling"]),
                MessageAttention.updated_at < self.started_at,
            )).all()
            for row in interrupted:
                phone = row.phone_submitted_at is not None
                self._set(
                    row, "phone_unknown" if phone else "reminder_unknown",
                    ["服务在请求提交期间重启，结果无法确认；不重复发送或呼叫。"],
                    "attention_interrupted",
                )
            self._discover(db, state)
            self.approvals.discover(db, state, utcnow(), self._set)
            candidate = db.scalar(
                select(MessageAttention).where(
                    MessageAttention.owner_open_id == owner.open_id,
                    MessageAttention.status.in_(ACTIVE),
                    MessageAttention.acknowledged_at.is_(None),
                    MessageAttention.reminder_submitted_at.is_(None),
                    MessageAttention.phone_submitted_at.is_(None),
                    or_(
                        MessageAttention.status.not_in(["assessing", "model_error"]),
                        MessageAttention.attempts < MAX_ATTEMPTS,
                    ),
                    or_(
                        MessageAttention.next_attempt_at.is_(None),
                        MessageAttention.next_attempt_at <= utcnow(),
                    ),
                ).order_by(
                    func.coalesce(
                        MessageAttention.next_attempt_at, MessageAttention.source_created_at
                    ),
                    MessageAttention.message_id,
                ).limit(1)
            )
            if candidate is None:
                return None
            if candidate.source_type == "message" and candidate.due_at < self.started_at:
                self._set(
                    candidate, "skipped_restart",
                    ["服务恢复时已超过提醒期限，未补打电话；可在工作台查看并标记已处理。"],
                )
                db.flush()
                return attention_view(candidate)
            message_id, status, source_type = candidate.message_id, candidate.status, candidate.source_type
        if source_type == "message" and status in {"queued", "assessing", "model_error"}:
            return self._assess(message_id)
        return self._escalate(message_id)

    def _discover_dms_history(self):
        """Include already cached DMS cards when this source is first enabled."""
        if not self._dms_history_pending:
            return
        with self.sessions.begin() as db:
            state, owner = db.get(AttentionState, 1), db.get(Account, 1)
            if not state or not owner or state.owner_open_id != owner.open_id:
                return
            rows = db.execute(
                select(Message, Chat).join(Chat, Chat.chat_id == Message.chat_id).where(
                    Chat.visible.is_(True),
                    Chat.chat_id == self.settings.dms_chat_id,
                ).order_by(Message.create_time.desc(), Message.message_id)
            ).yield_per(200)
            now = utcnow()
            for message, chat in rows:
                self.dms.observe(db, state, chat, message, now, self._set)
        self._dms_history_pending = False

    def _discover(self, db, state):
        rows = db.execute(
            select(Message, Chat).join(Chat, Chat.chat_id == Message.chat_id).where(
                Chat.visible.is_(True),
                or_(
                    Message.create_time >= int(state.activated_at.timestamp() * 1000),
                    Chat.chat_id == getattr(self.settings, "dms_chat_id", "")
                    if self.dms.configured else False,
                ),
                tuple_(Message.synced_at, Message.message_id)
                > tuple_(state.discovery_at, state.discovery_message_id),
            ).order_by(Message.synced_at, Message.message_id).limit(200)
        ).all()
        for message, chat in rows:
            if self.dms.observe(db, state, chat, message, utcnow(), self._set):
                continue
            existing = db.get(MessageAttention, message.message_id)
            raw = message.raw
            if (
                not isinstance(raw, dict)
                or raw.get("message_id") != message.message_id
                or raw.get("chat_id") != message.chat_id
            ):
                continue
            if existing and (
                existing.acknowledged_at or existing.reminder_submitted_at or existing.phone_submitted_at
            ):
                continue
            if not is_personal_mention(raw, state.owner_open_id, self.settings.feishu_app_id):
                if existing and existing.status in ACTIVE:
                    self._set(existing, "source_unavailable", ["原消息已撤回或不再明确 @ 本人。"])
                continue
            digest = source_hash(raw)
            if existing and existing.source_hash == digest:
                continue
            if existing is None:
                existing = MessageAttention(
                    message_id=message.message_id, owner_open_id=state.owner_open_id,
                    chat_id=message.chat_id, chat_name=chat.name, assessment_cache={},
                )
                db.add(existing)
            existing.chat_name = chat.name
            existing.text = message_text(raw)
            existing.source_hash = digest
            existing.source_created_at = _timestamp(raw)
            existing.due_at = existing.source_created_at + timedelta(
                seconds=self.settings.attention_timeout_seconds
            )
            existing.attempts, existing.model_result = 0, {}
            existing.context_hash, existing.assessment_context = None, []
            self._set(existing, "queued", ["等待判断消息是否明确重要且需要本人响应。"])
        if rows:
            last = rows[-1][0]
            state.discovery_at, state.discovery_message_id = last.synced_at, last.message_id

    @staticmethod
    def _set(row, status, reasons, error=None, next_attempt=None):
        row.status, row.reasons, row.error_code = status, reasons, error
        row.next_attempt_at, row.updated_at = next_attempt, utcnow()

    def _assess(self, message_id):
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            if row.acknowledged_at or row.reminder_submitted_at:
                return attention_view(row)
            source = db.get(Message, message_id)
            if not source or source.deleted or source_hash(source.raw) != row.source_hash:
                self._set(row, "source_changed", ["原消息已更新，等待同步后重新判断。"])
                return attention_view(row)
            if row.context_hash is None:
                previous = list(reversed(db.scalars(
                    select(Message).where(
                        Message.chat_id == row.chat_id,
                        Message.create_time < source.create_time, Message.deleted.is_(False),
                    ).order_by(Message.create_time.desc(), Message.message_id.desc()).limit(8)
                ).all()))
                following = db.scalars(
                    select(Message).where(
                        Message.chat_id == row.chat_id,
                        Message.create_time > source.create_time, Message.deleted.is_(False),
                    ).order_by(Message.create_time, Message.message_id).limit(93)
                ).all()
                if len(following) > 92:
                    self._set(
                        row, "source_unavailable",
                        ["后续讨论过多，无法在完整上下文中可靠判断，未触发电话。"],
                        "attention_context_too_large",
                    )
                    return attention_view(row)
                row.assessment_context = [_model_message(item.raw) for item in [*previous, *following]]
                row.context_hash = _context_hash(row.assessment_context)
            cache_key = hashlib.sha256(
                f"{row.source_hash}:{row.context_hash}:{MODEL}:{EFFORT}:{PROMPT_VERSION}".encode()
            ).hexdigest()
            cached = (row.assessment_cache or {}).get(cache_key)
            if cached:
                self._apply_result(row, cached)
                return attention_view(row)
            if row.attempts >= MAX_ATTEMPTS:
                return attention_view(row)
            payload = {
                "message": _model_message(source.raw),
                "context": row.assessment_context,
            }
            row.attempts += 1
            snapshot, context_snapshot, attempt = row.source_hash, row.context_hash, row.attempts
            self._set(
                row, "assessing", ["正在判断消息的重要性。"],
                next_attempt=utcnow() + timedelta(
                    seconds=max(300, getattr(self.settings, "approval_model_timeout_seconds", 180) + 60)
                ),
            )
        try:
            result = _verified_result(self.provider.assess(payload), payload)
            error = None
        except Exception as exc:
            result, error = None, _safe_error(exc)
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            if (
                row.acknowledged_at or row.source_hash != snapshot or row.context_hash != context_snapshot
                or row.attempts != attempt or row.status != "assessing"
            ):
                return attention_view(row)
            if error:
                if error == "attention_model_busy":
                    # The shared host did not start inference. Contention with
                    # approvals must not exhaust this message's failure budget.
                    row.attempts -= 1
                    self._set(
                        row, "queued", ["模型正在处理其他任务，稍后判断本条消息。"], error,
                        utcnow() + timedelta(seconds=60),
                    )
                else:
                    self._set(
                        row, "model_error", ["模型暂时无法可靠判断，未触发电话提醒。"], error,
                        utcnow() + timedelta(seconds=60 * 2 ** attempt)
                        if attempt < MAX_ATTEMPTS else None,
                    )
            else:
                row.assessment_cache = {**(row.assessment_cache or {}), cache_key: result}
                self._apply_result(row, result)
            db.flush()
            return attention_view(row)

    @staticmethod
    def _apply_result(row, result):
        row.model_result, row.assessed_at = result, utcnow()
        important = result["importance"] == "high" and result["requires_response"]
        AttentionService._set(
            row, "waiting" if important else "normal",
            result["reasons"],
            next_attempt=row.due_at if important else None,
        )

    def _block(self, message_id, status, reasons, error=None, *, retry=False):
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            if row.acknowledged_at:
                return attention_view(row)
            self._set(
                row, status, reasons, error,
                utcnow() + timedelta(seconds=60) if retry else None,
            )
            db.flush()
            return attention_view(row)

    def _fresh_check(self, row):
        if row.source_type == "approval":
            return self.approvals.fresh_check(row)
        if row.source_type == "dms_card":
            return self.dms.fresh_check(row)
        with self.sessions() as db:
            owner, chat = db.get(Account, 1), db.get(Chat, row.chat_id)
            current = db.get(MessageAttention, row.message_id)
            if (
                not owner or owner.open_id != row.owner_open_id
                or not chat or not chat.visible or current.acknowledged_at
            ):
                return "responded", ["已标记处理或当前账号无法继续访问该消息。"], None
        raw = self.feishu.message(row.message_id)
        if (
            not isinstance(raw, dict) or raw.get("message_id") != row.message_id
            or raw.get("chat_id") != row.chat_id
            or not is_personal_mention(raw, row.owner_open_id, self.settings.feishu_app_id)
        ):
            return "source_unavailable", ["原消息已撤回或不能确认仍然 @ 本人，未触发电话。"], None
        if source_hash(raw) != row.source_hash:
            return "source_changed", ["原消息内容已变化，等待同步后重新判断。"], None
        statuses = self.feishu.message_read_status([row.message_id])
        read = statuses.get(row.message_id) if isinstance(statuses, dict) else None
        if read is True:
            return "responded", ["飞书显示原消息已读，按已响应处理，不再电话提醒。"], None
        if read is not False:
            return "blocked", ["暂时无法确认原消息是否未读，未触发电话。"], "attention_read_unknown"
        previous = [
            item for item in (row.assessment_context or [])
            if _timestamp(item) and _timestamp(item) < row.source_created_at
        ]
        if len(previous) > 8:
            return "blocked", ["参与判断的前文范围异常，未触发电话。"], "attention_context_invalid"
        refreshed = []
        for item in previous:
            live = self.feishu.message(item["message_id"])
            if (
                not isinstance(live, dict) or live.get("message_id") != item["message_id"]
                or live.get("chat_id") != row.chat_id or _timestamp(live) != _timestamp(item)
            ):
                return "blocked", ["无法确认参与判断的前文仍有效，未触发电话。"], "attention_context_invalid"
            if not live.get("deleted"):
                refreshed.append(_model_message(live))

        # A chat listing contains only thread roots. Fetch the source's own
        # thread separately, with pagination and local timestamp filtering;
        # Feishu does not support start_time/end_time for a thread container.
        source_thread = raw.get("thread_id")
        if source_thread == "":
            source_thread = None
        if source_thread is not None and (
            not isinstance(source_thread, str) or not source_thread
        ):
            return "blocked", ["原消息话题标识无法核实，未触发电话。"], "attention_thread_invalid"
        containers = [("chat", row.chat_id)]
        if source_thread:
            containers.append(("thread", source_thread))
        subsequent = {}
        for container_type, container_id in containers:
            parameters = {"container_id_type": container_type}
            if container_type == "chat":
                parameters["start_time"] = str(int(row.source_created_at.timestamp()))
            page_token, seen, complete = None, set(), False
            for _ in range(TAIL_PAGE_LIMIT):
                listing = self.feishu.messages(container_id, parameters, page_token)
                if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
                    return "blocked", ["无法完整核验后续回复，未触发电话。"], "attention_tail_invalid"
                for item in listing["items"]:
                    created = _timestamp(item) if isinstance(item, dict) else None
                    if not created or item.get("chat_id") != row.chat_id:
                        return "blocked", ["后续回复所属会话或时间无法核实。"], "attention_tail_invalid"
                    if item.get("message_id") == row.message_id and item.get("thread_id"):
                        discovered_thread = item["thread_id"]
                        if not isinstance(discovered_thread, str) or (
                            source_thread and source_thread != discovered_thread
                        ):
                            return "blocked", ["原消息话题标识不一致。"], "attention_thread_invalid"
                        if not source_thread:
                            source_thread = discovered_thread
                            containers.append(("thread", source_thread))
                    if created > row.source_created_at and _sender_is(item, row.owner_open_id):
                        # A later message in this chat or source thread suffices
                        # to suppress escalation, even if relevance is uncertain.
                        return "responded", ["本人已在该会话或话题后续发言，停止电话提醒。"], None
                    if (
                        created > row.source_created_at
                        and (item.get("sender") or {}).get("sender_type") == "user"
                        and (item.get("sender") or {}).get("id_type") != "open_id"
                    ):
                        return "blocked", ["后续回复身份无法核实，未触发电话。"], "attention_tail_invalid"
                    if created > row.source_created_at and item.get("message_id") != row.message_id:
                        identifier = item.get("message_id")
                        if not isinstance(identifier, str) or not identifier:
                            return "blocked", ["后续回复标识无法核实，未触发电话。"], "attention_tail_invalid"
                        if not item.get("deleted"):
                            subsequent[identifier] = _model_message(item)
                if listing.get("has_more") is False:
                    complete = True
                    break
                page_token = listing.get("page_token")
                if not isinstance(page_token, str) or not page_token or page_token in seen:
                    break
                seen.add(page_token)
            if not complete:
                return "blocked", ["后续会话或话题分页尚未完整核验，未触发电话。"], "attention_tail_incomplete"
        context = refreshed + sorted(
            subsequent.values(), key=lambda item: (int(item["create_time"]), item["message_id"])
        )
        if len(context) > 100:
            return "blocked", ["后续讨论过多，无法完整判断是否仍需响应。"], "attention_context_too_large"
        if _context_hash(context) != row.context_hash:
            with self.sessions.begin() as db:
                current = db.get(MessageAttention, row.message_id, with_for_update=True)
                if current.acknowledged_at:
                    return "responded", ["本人已标记处理，停止电话提醒。"], None
                if current.reminder_submitted_at:
                    return "blocked", ["提醒发送后会话有新进展，未继续提交电话。"], "attention_context_changed"
                current.assessment_context = context
                current.context_hash = _context_hash(context)
                current.attempts, current.model_result = 0, {}
                self._set(current, "queued", ["会话有新进展，等待模型重新判断是否仍需响应。"])
            return "queued", ["会话有新进展，等待模型重新判断是否仍需响应。"], None
        return None, [], None

    def _source_changed_before_submission(self, db, row):
        if row.source_type == "dms_card":
            # Keep a concurrent card refresh from racing the durable submission marker.
            source_id = (row.source_data or {}).get("source_message_id")
            if source_id:
                db.get(Message, source_id, with_for_update=True, populate_existing=True)
            status, reasons, error = self.dms.local_check(db, row)
            if status:
                self._set(
                    row, status, reasons, error,
                    utcnow() + timedelta(seconds=60) if status == "blocked" else None,
                )
                return True
            return False
        if row.source_type != "approval":
            return False
        # Feedback and approval execution both lock this review. Serialize the
        # durable outbound marker against those changes, without a network call
        # inside the transaction. A later response cannot cancel a submitted call.
        db.get(ApprovalReview, row.approval_task_id, with_for_update=True)
        status, reasons, error = self.approvals.local_check(db, row)
        if status:
            self._set(
                row, status, reasons, error,
                utcnow() + timedelta(seconds=60) if status == "blocked" else None,
            )
            return True
        return False

    def _escalate(self, message_id):
        with self.sessions() as db:
            row = db.get(MessageAttention, message_id)
            if row.due_at > utcnow() or row.acknowledged_at or row.reminder_submitted_at:
                return attention_view(row)
            if row.source_type == "message" and (
                row.model_result.get("importance") != "high"
                or row.model_result.get("requires_response") is not True
            ):
                return attention_view(row)
        try:
            permissions = self.feishu.attention_permissions()
            if not isinstance(permissions, dict) or permissions.get("ready") is not True:
                return self._block(
                    message_id, "blocked_permission",
                    ["缺少飞书机器人发送或电话加急权限，开通后将重新核验。"],
                    "attention_permission_missing", retry=True,
                )
            status, reasons, error = self._fresh_check(row)
            if status:
                return self._block(message_id, status, reasons, error, retry=status == "blocked")
        except Exception as exc:
            return self._block(
                message_id, "blocked", ["未能完成发送前核验，未触发电话。"],
                _safe_error(exc), retry=True,
            )
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            state = db.get(AttentionState, 1, with_for_update=True)
            if row.acknowledged_at or row.reminder_submitted_at or row.phone_submitted_at:
                return attention_view(row)
            if self._source_changed_before_submission(db, row):
                return attention_view(row)
            if state.last_call_submitted_at and state.last_call_submitted_at + timedelta(
                seconds=self.settings.attention_call_cooldown_seconds
            ) > utcnow():
                self._set(
                    row, "waiting", ["近期已向本人提交过电话提醒，等待 30 分钟间隔后再核验。"],
                    next_attempt=state.last_call_submitted_at + timedelta(
                        seconds=self.settings.attention_call_cooldown_seconds
                    ),
                )
                return attention_view(row)
            row.reminder_uuid = str(uuid4())
            row.reminder_submitted_at = utcnow()
            self._set(row, "reminder_sending", ["正在发送仅本人可见的消息提醒。"])
            reminder_uuid = row.reminder_uuid
            # Do not send source text or model-generated instructions to anyone
            # else. FeishuClient fixes the recipient to the bound account.
            if row.source_type == "approval":
                reminder = self.approvals.reminder_text(row)
            elif row.source_type == "dms_card":
                reminder = self.dms.reminder_text(row)
            else:
                reminder = (
                    f"重要消息提醒：{row.chat_name}\n"
                    f"有一条明确 @ 你的重要消息，超过 {self.settings.attention_timeout_seconds // 60} 分钟"
                    "仍显示未读且未发现你的后续回复。\n"
                    f"{row.model_result.get('summary', '')[:1000]}\n"
                    "打开原会话：https://applink.feishu.cn/client/chat/open?"
                    f"{urlencode({'openChatId': row.chat_id})}\n"
                    "请打开原会话查看；已读、回复或在工作台标记已处理后停止提醒。"
                )
        try:
            response = self.feishu.send_self_reminder(reminder, reminder_uuid)
            reminder_id = response.get("message_id") if isinstance(response, dict) else None
            if not isinstance(reminder_id, str) or not 1 <= len(reminder_id) <= 128:
                raise AppError("attention_reminder_invalid", "提醒发送结果无法确认。")
        except Exception as exc:
            return self._block(
                message_id, "reminder_unknown",
                ["消息提醒的发送结果未确认，未提交电话；为避免重复提醒，不自动重试。"],
                _safe_error(exc),
            )
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            row.reminder_message_id = reminder_id
            row.updated_at = utcnow()
            if row.acknowledged_at:
                return attention_view(row)
        # Reading the original, replying, or acknowledging while the reminder
        # was sent must still suppress the phone request.
        try:
            status, reasons, error = self._fresh_check(row)
            if status:
                return self._block(message_id, status, reasons, error)
        except Exception as exc:
            return self._block(
                message_id, "blocked", ["提醒已发出，但最终响应核验失败，未提交电话。"],
                _safe_error(exc),
            )
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            state = db.get(AttentionState, 1, with_for_update=True)
            owner = db.get(Account, 1)
            if (
                row.acknowledged_at or row.phone_submitted_at
                or not owner or owner.open_id != row.owner_open_id
                or state.owner_open_id != row.owner_open_id
            ):
                return attention_view(row)
            if self._source_changed_before_submission(db, row):
                return attention_view(row)
            if state.last_call_submitted_at and state.last_call_submitted_at + timedelta(
                seconds=self.settings.attention_call_cooldown_seconds
            ) > utcnow():
                self._set(row, "blocked", ["近期已有电话提醒，本条消息未再提交电话。"])
                return attention_view(row)
            # This commit is the irreversible single-attempt boundary. Recovery
            # never retries a row with either outbound marker, including crashes.
            row.phone_submitted_at = state.last_call_submitted_at = utcnow()
            self._set(row, "calling", ["电话提醒请求正在提交；提交结果尚未确认。"])
        try:
            result = self.feishu.phone_self(reminder_id)
            if (
                not isinstance(result, dict)
                or result.get("invalid_user_id_list") != []
            ):
                raise AppError("attention_phone_invalid", "电话提醒受理结果无法确认。")
        except Exception as exc:
            return self._block(
                message_id, "phone_unknown",
                ["电话提醒提交结果未确认；为避免重复呼叫，不自动重试。"],
                _safe_error(exc),
            )
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            row.phone_accepted_at = utcnow()
            if not row.acknowledged_at:
                self._set(row, "phone_accepted", ["飞书已受理电话提醒请求，无法据此确认本人已接听。"])
            db.flush()
            return attention_view(row)

    def acknowledge(self, message_id):
        with self.sessions.begin() as db:
            row = db.get(MessageAttention, message_id, with_for_update=True)
            owner = db.get(Account, 1)
            if not row or not owner or row.owner_open_id != owner.open_id:
                raise AppError("attention_not_found", "未找到当前账号的提醒记录。", 404)
            if row.acknowledged_at is None:
                row.acknowledged_at = utcnow()
                self._set(
                    row, "acknowledged",
                    ["本人已标记处理。电话请求已经提交，无法撤回。"]
                    if row.phone_submitted_at else ["本人已标记处理，停止后续电话提醒。"],
                )
            db.flush()
            return attention_view(row)
