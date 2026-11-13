"""Read-only DMS enrichment, independently scheduled from reminders and delivery."""

import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import select, text, tuple_

from app.dms_attention import DmsCardAttentionSource, _parse, dms_attention_id
from app.dms_risk import assess_dms_detail
from app.errors import AppError
from app.models import Account, Chat, DmsReview, Message, MessageAttention, SyncState, utcnow

LOCK_ID = 81024007
REFRESH_SECONDS = 300
RESOURCE = "dms_details"
CARD_TYPES = {
    "数据变更": "DC_COMMON", "普通数据变更": "DC_COMMON", "普通变更": "DC_COMMON",
    "Data Correct": "DC_COMMON",
    "权限": "PERM_APPLY", "数据导出": "DATA_EXPORT", "数据库导出": "DB_EXPORT",
    "无锁变更": "DC_CHUNK", "历史数据清理": "DC_CRON_CLEAR",
    "存储过程变更": "DC_PROC", "数据导入": "DC_BIG_FILE",
}


def pending_review():
    return {"status": "pending", "summary": "正在调取 DMS 工单详情并核验变更风险。"}


def review_view(db, settings, owner, identifier, card):
    """Never project a verdict for another owner, card version, or completed task."""
    if not getattr(settings, "dms_details_enabled", False) or card["status"] != "pending":
        return None
    row = db.get(DmsReview, identifier, populate_existing=True)
    if (
        not row or row.owner_open_id != owner or row.source_hash != card["hash"]
        or row.task_key != card["task_key"] or not row.active or not row.result
        or not row.fetched_at or row.fetched_at + timedelta(seconds=REFRESH_SECONDS) <= utcnow()
    ):
        return pending_review()
    return {**row.result, "fetched_at": row.fetched_at.isoformat(), "source_url": card.get("source_url", "")}


def attention_review(db, settings, row):
    if row.source_type != "dms_card" or row.acknowledged_at or row.status == "responded":
        return None
    owner = db.get(Account, 1)
    source = DmsCardAttentionSource(settings, None, None)
    chat = db.get(Chat, row.chat_id)
    message = db.get(Message, (row.source_data or {}).get("source_message_id"))
    if not owner or row.owner_open_id != owner.open_id or not message or not source._origin(message.raw, owner, chat):
        return None
    card = _parse(message.raw)
    if not card or card["task_key"] != (row.source_data or {}).get("task_key"):
        return None
    card = source._latest(db, owner, chat, card)
    return review_view(db, settings, owner.open_id, row.message_id, card)


def order_reference(raw, card):
    """A taskId alone is not proof that a matching numeric OrderId is this order."""
    body = raw["body"]["content"]
    body = json.loads(body) if isinstance(body, str) else body
    url = body["card_link"]["url"]
    parsed = urlsplit(url)
    queries = [parsed.query, parsed.fragment.partition("?")[2], parsed.fragment]
    mobile_order = (
        parsed.scheme == "https" and parsed.hostname == "dms.aliyun.com"
        and parsed.username is None and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path in {"/console/mobile/index", "/console/mobile/index/"}
    )
    values = []
    for query in queries:
        for key, candidates in parse_qs(query).items():
            if key.lower() in {"orderid", "ticketid"} or mobile_order and key.lower() == "pid":
                values.extend(candidates)
    if values and (len(set(values)) != 1 or not re.fullmatch(r"[0-9]{1,32}", values[0])):
        raise AppError("dms_order_identity_mismatch", "DMS 工单链接中的标识不一致。", 409)
    if values and values[0] != card["task_id"]:
        raise AppError("dms_order_identity_mismatch", "DMS 工单链接与卡片标识不一致。", 409)
    return card["task_id"], bool(values)


class DmsReviewService:
    def __init__(self, settings, sessions, client):
        self.settings, self.sessions, self.client = settings, sessions, client
        self.source = DmsCardAttentionSource(settings, sessions, None)

    def tick(self):
        if not self.settings.dms_details_enabled or not self.source.configured:
            return None
        with self.sessions() as db:
            engine = db.get_bind()
        with engine.connect() as guard:
            if not guard.scalar(text(f"SELECT pg_try_advisory_lock({LOCK_ID})")):
                guard.commit()
                return None
            guard.commit()
            try:
                return self._tick()
            finally:
                guard.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
                guard.commit()

    def _current(self, db, owner, message_id):
        chat = db.get(Chat, self.source.chat_id, populate_existing=True)
        message = db.get(Message, message_id, populate_existing=True)
        if (
            not message or message.deleted or message.chat_id != self.source.chat_id
            or not self.source._origin(message.raw, owner, chat)
        ):
            return None
        card = _parse(message.raw)
        if not card or card["message_id"] != message.message_id:
            return None
        latest = self.source._latest(db, owner, chat, card)
        if latest["status"] != "pending":
            return None
        latest_message = db.get(Message, latest["message_id"], populate_existing=True)
        return (latest, latest_message) if latest_message and latest_message.chat_id == self.source.chat_id and not latest_message.deleted else None

    def _discover(self, db, owner, now):
        state = db.get(SyncState, RESOURCE, with_for_update=True)
        initial = not state or state.parameters.get("owner") != owner.open_id
        if initial:
            if not state:
                state = SyncState(resource=RESOURCE, generation=str(uuid4()), parameters={})
                db.add(state)
            state.parameters = {"owner": owner.open_id, "message_id": ""}
            state.synced_at = now - timedelta(days=1)
        rows = db.scalars(select(Message).where(
            Message.chat_id == self.source.chat_id,
            tuple_(Message.synced_at, Message.message_id)
            > tuple_(state.synced_at, state.parameters.get("message_id", "")),
        ).order_by(Message.synced_at, Message.message_id).limit(200)).all()
        # Existing pending reminders may predate the recent message cursor.
        tracked = []
        if initial:
            pending_ids = select(MessageAttention.source_data["source_message_id"].as_string()).where(
                MessageAttention.source_type == "dms_card", MessageAttention.owner_open_id == owner.open_id,
                MessageAttention.status.not_in(["responded", "acknowledged"]),
                MessageAttention.acknowledged_at.is_(None),
            )
            tracked = db.scalars(select(Message).where(
                Message.chat_id == self.source.chat_id, Message.message_id.in_(pending_ids),
            )).all()
        for message in [*tracked, *rows]:
            current = self._current(db, owner, message.message_id)
            if not current:
                continue
            card, latest = current
            identifier = dms_attention_id(self.source.app_id, self.source.chat_id, card["task_key"], owner.open_id)
            row = db.get(DmsReview, identifier)
            if not row:
                row = DmsReview(
                    source_id=identifier, owner_open_id=owner.open_id, task_key=card["task_key"],
                    source_message_id=latest.message_id, source_hash=card["hash"],
                    active=True, result={}, attempts=0, next_attempt_at=now, updated_at=now,
                )
                db.add(row)
            else:
                row.source_message_id = latest.message_id
                if row.source_hash != card["hash"] or not row.active:
                    row.source_hash, row.active = card["hash"], True
                    row.result, row.attempts, row.fetched_at = {}, 0, None
                    row.next_attempt_at, row.updated_at = now, now
        if rows:
            state.synced_at = rows[-1].synced_at
            state.parameters = {"owner": owner.open_id, "message_id": rows[-1].message_id}

    def _tick(self):
        now = utcnow()
        with self.sessions.begin() as db:
            owner = db.get(Account, 1)
            if not owner:
                return None
            self._discover(db, owner, now)
            candidates = db.scalars(select(DmsReview).where(
                DmsReview.owner_open_id == owner.open_id, DmsReview.active.is_(True),
                DmsReview.next_attempt_at <= now,
            ).order_by(DmsReview.next_attempt_at, DmsReview.source_id).limit(20)).all()
            chosen = None
            for row in candidates:
                current = self._current(db, owner, row.source_message_id)
                if not current or current[0]["task_key"] != row.task_key:
                    row.active, row.result = False, {}
                    continue
                card, message = current
                if card["hash"] != row.source_hash:
                    row.result, row.fetched_at, row.attempts = {}, None, 0
                    row.source_hash = card["hash"]
                row.source_message_id = message.message_id
                row.attempts += 1
                # Reserve a retry before I/O. A crashed process can resume later.
                row.next_attempt_at = now + timedelta(seconds=60)
                chosen = (row.source_id, owner.open_id, card, message.raw, row.attempts)
                break
        if not chosen:
            return None
        identifier, owner_id, card, raw, attempts = chosen
        try:
            order_id, verified_reference = order_reference(raw, card)
            snapshot = self.client.fetch(order_id)
            if not isinstance(snapshot, dict) or str(snapshot.get("order_id")) != order_id:
                raise AppError("dms_order_identity_mismatch", "DMS 工单身份无法核实。", 502)
            base = snapshot.get("base")
            if not isinstance(base, dict) or str(base.get("OrderId")) != order_id:
                raise AppError("dms_order_identity_mismatch", "DMS 工单身份无法核实。", 502)
            expected_type = CARD_TYPES.get(card["task_kind"].removesuffix("申请"))
            if not verified_reference or not expected_type or base.get("PluginType") != expected_type:
                # Do not present a candidate order's facts as this card's detail.
                error = "dms_order_reference_unverified" if not verified_reference else "dms_order_type_unverified"
                snapshot = {
                    "order_id": order_id, "base": {"OrderId": order_id}, "detail": {},
                    "sql_checks": [], "complete": False, "errors": [error],
                }
            result = assess_dms_detail(snapshot)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "dms_details_unavailable"
            code = code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code) else "dms_details_unavailable"
            result = {
                "status": "unavailable", "risk": "high",
                "summary": "尚未取得可核实的 DMS 工单详情，按高危处理（信息不足，待核查）。",
                "reasons": ["详情读取失败或工单身份无法核实，不能判断为低危。"],
                "missing_evidence": ["完整工单详情及变更范围"], "facts": {}, "error_code": code,
            }
        with self.sessions.begin() as db:
            owner = db.get(Account, 1, populate_existing=True)
            row = db.get(DmsReview, identifier, with_for_update=True)
            current = self._current(db, owner, row.source_message_id) if row and owner and owner.open_id == owner_id else None
            if (
                not current or current[0]["task_key"] != row.task_key
                or current[0]["hash"] != card["hash"] or row.source_hash != card["hash"]
            ):
                return {"status": "discarded"}
            row.result, row.fetched_at, row.updated_at = result, utcnow(), utcnow()
            delay = REFRESH_SECONDS if result.get("status") == "ready" else min(600, 60 * 2 ** min(attempts - 1, 4))
            row.next_attempt_at = utcnow() + timedelta(seconds=delay)
        return {"status": result["status"], "risk": result["risk"]}
