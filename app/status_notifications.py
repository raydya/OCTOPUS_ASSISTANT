"""Owner-only status DMs with durable, single-attempt delivery.

This observer neither makes business decisions nor changes phone escalation.
It runs independently of slow model requests and never replays ambiguous sends.
"""

import hashlib
import json
import re
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, or_, select, text, tuple_, update

from app.approval_completion import completed_owner_task_status
from app.attention import is_personal_mention, source_hash
from app.dms_attention import (
    TERMINAL,
    DmsCardAttentionSource,
    _metadata,
    _parse,
    _presentation,
    dms_attention_id,
)
from app.errors import AppError
from app.model_review import business_snapshot
from app.models import (
    Account,
    ApprovalInstance,
    ApprovalReview,
    ApprovalTask,
    Chat,
    Message,
    MessageAttention,
    ModelAssessment,
    StatusNoticeSource,
    StatusNotification,
    StatusNotificationState,
    utcnow,
)
from app.status_notice_content import (
    approval_notice,
    attention_notice,
    compact_text,
    dms_receipt_revision,
    dms_received_notice,
    mention_received_notice,
    status_card,
)

LOCK_ID = 81024006
UNSENT = {"queued", "blocked_permission"}
SEND_LIMIT = 3
MESSAGE_EXPIRED = "status_message_expired"


def source_key(owner, kind, identifier):
    return hashlib.sha256(json.dumps([owner, kind, identifier]).encode()).hexdigest()


def _source_revision(row):
    return row.source_revision or row.revision


def notification_view(row):
    return {key: getattr(row, key) for key in (
        "id", "source_key", "phase", "text", "status", "submitted_at", "sent_at",
        "message_id", "error_code", "created_at", "updated_at",
        "delivery_action",
    )}


def _safe_error(exc):
    code = exc.code if isinstance(exc, AppError) else "status_notification_unavailable"
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", code) else "status_notification_unavailable"


class StatusNotificationService:
    def __init__(self, settings, sessions, feishu):
        self.settings, self.sessions, self.feishu = settings, sessions, feishu
        self.dms = DmsCardAttentionSource(settings, sessions, feishu)

    def tick(self):
        if not getattr(self.settings, "status_notifications_enabled", False):
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

    def _approval(self, db, task, owner):
        raw = task.raw
        if (
            not isinstance(raw, dict) or raw.get("user_id") != owner
            or raw.get("task_id") != task.task_id or raw.get("instance_code") != task.instance_code
        ):
            return None
        review = db.get(ApprovalReview, task.task_id, populate_existing=True)
        assessment = db.scalar(select(ModelAssessment).where(
            ModelAssessment.task_id == task.task_id,
            ModelAssessment.instance_code == task.instance_code,
            ModelAssessment.owner_open_id == owner,
        ).order_by(ModelAssessment.created_at.desc(), ModelAssessment.id.desc()).limit(1))
        detail = db.get(ApprovalInstance, task.instance_code, populate_existing=True)
        snapshot = business_snapshot(raw, detail.raw, owner) if detail else ""
        # A task may return to pending after rollback. Older instance data
        # cannot establish the outcome of the newer task-list observation.
        completion_detail = detail.raw if detail and detail.synced_at >= task.synced_at else None
        payload = approval_notice(
            task, review, assessment, base_url=self.settings.app_base_url,
            current_snapshot_hash=snapshot, instance_detail=completion_detail,
        )
        if completed_owner_task_status(
            completion_detail, instance_code=task.instance_code, task_id=task.task_id,
            owner_open_id=owner,
        ):
            source = db.get(StatusNoticeSource, source_key(owner, "approval", task.task_id))
            if (
                source and source.card_create_state != "ready"
                and source.revision != payload["revision"]
                and (
                    detail.synced_at <= source.updated_at
                    or detail.synced_at + timedelta(seconds=self.settings.attention_timeout_seconds) <= utcnow()
                )
            ):
                # A newly supported display mapping must not re-announce old
                # legacy text history. Only recent new reads are evidence; after
                # _observe records it, matching revisions preserve that same
                # evidence during _deliver's second projection.
                return approval_notice(
                    task, review, assessment, base_url=self.settings.app_base_url,
                    current_snapshot_hash=snapshot,
                )
        return payload

    def _mention(self, db, message, chat, owner):
        if (
            not chat or not chat.visible or message.deleted
            or not isinstance(message.raw, dict)
            or message.raw.get("message_id") != message.message_id
            or message.raw.get("chat_id") != message.chat_id
            or not is_personal_mention(message.raw, owner, self.settings.feishu_app_id)
        ):
            return None
        row = db.get(MessageAttention, message.message_id, populate_existing=True)
        submitted_result, suppress = False, False
        if (
            row and row.source_type == "message" and row.owner_open_id == owner
            and row.chat_id == message.chat_id and row.source_hash == source_hash(message.raw)
        ):
            payload = attention_notice(row, base_url=self.settings.app_base_url)
            suppress = row.status == "skipped_restart"
            # Preserve facts about actual outbound attempts, even if their
            # result notification waits longer than an ordinary status update.
            submitted_result = bool(
                row.phone_submitted_at and row.status in {"calling", "phone_unknown", "phone_accepted"}
                or row.reminder_submitted_at and row.status in {"reminder_sending", "reminder_unknown"}
            )
        else:
            payload = mention_received_notice(message, chat, base_url=self.settings.app_base_url)
        return {
            **payload,
            "message_expired": message.create_time + self.settings.attention_timeout_seconds * 1000
            <= int(utcnow().timestamp() * 1000),
            "suppress_notification": suppress,
            "submitted_result": submitted_result,
        }

    def _refresh(self, db, source, owner):
        if source.source_type == "approval":
            task = db.get(ApprovalTask, (source.source_id, "1"), populate_existing=True)
            return self._approval(db, task, owner) if task else None
        if source.source_type == "message":
            message = db.get(Message, source.source_id, populate_existing=True)
            chat = db.get(Chat, message.chat_id, populate_existing=True) if message else None
            return self._mention(db, message, chat, owner) if message else None
        row = db.get(MessageAttention, source.source_id, populate_existing=True)
        if row:
            return self._dms_notice(db, row, owner)
        origin_id = (source.payload or {}).get("origin_message_id")
        message = db.get(Message, origin_id, populate_existing=True) if origin_id else None
        chat = db.get(Chat, message.chat_id) if message else None
        return self._dms_message(db, message, chat, owner) if message else None

    def _dms_message(self, db, message, chat, owner):
        account = db.get(Account, 1)
        if not account or account.open_id != owner or not self.dms._origin(message.raw, account, chat):
            return None
        card = _parse(message.raw)
        if not card or card["message_id"] != message.message_id:
            return None
        identifier = dms_attention_id(self.dms.app_id, self.dms.chat_id, card["task_key"], owner)
        row = db.get(MessageAttention, identifier, populate_existing=True)
        if row:
            return self._dms_notice(db, row, owner)
        card = self.dms._latest(db, account, chat, card)
        status = "responded" if card["status"] in TERMINAL else "waiting" if card["status"] == "pending" else "blocked"
        view = SimpleNamespace(
            message_id=identifier, source_type="dms_card", chat_id=chat.chat_id,
            status=status, acknowledged_at=None, source_data=_metadata(card, self.dms.app_id, chat.chat_id),
            model_result=_presentation(card), reasons=[], text=card["task_key"],
        )
        result = attention_notice(view, base_url=self.settings.app_base_url)
        result["origin_message_id"] = card["message_id"]
        return result

    def _dms_notice(self, db, row, owner):
        data = row.source_data or {}
        account = db.get(Account, 1)
        chat = db.get(Chat, row.chat_id, populate_existing=True)
        message = db.get(Message, data.get("source_message_id"), populate_existing=True) if data.get("source_message_id") else None
        if (
            row.source_type != "dms_card" or row.owner_open_id != owner
            or not account or account.open_id != owner
            or not message or message.chat_id != row.chat_id
            or data.get("sender_app_id") != self.dms.app_id or data.get("chat_id") != self.dms.chat_id
            or not self.dms._origin(message.raw, account, chat)
        ):
            return None
        card = _parse(message.raw)
        if not card or card["message_id"] != message.message_id or card["task_key"] != data.get("task_key"):
            return None
        card = self.dms._latest(db, account, chat, card)
        # Project a copy: status DMs must never mutate the phone reminder state.
        view = SimpleNamespace(**{column.name: getattr(row, column.name) for column in MessageAttention.__table__.columns})
        if not row.acknowledged_at:
            if card["status"] in TERMINAL:
                view.status = "responded"
                view.source_data = {**data, "card_status": card["status"]}
                view.reasons = ["已同步的原卡片显示处理结束。"]
            elif card["status"] != "pending" or message.deleted:
                view.status, view.reasons = "blocked", ["原卡片状态暂时无法可靠识别。"]
            elif card["hash"] != row.source_hash:
                view.status, view.reasons = "source_changed", ["原卡片内容已变化，等待重新核验。"]
        result = attention_notice(view, base_url=self.settings.app_base_url)
        result["origin_message_id"] = card["message_id"]
        return result

    def _new_dms_receipt(self, db, owner, payload):
        if payload["source_type"] != "dms_card":
            return None
        state = db.get(StatusNotificationState, 1)
        origin_id = payload.get("origin_message_id")
        message = db.get(Message, origin_id) if origin_id else None
        # A newly created phone tracking row can represent an old card. Use the
        # original message time, not observer or phone worker discovery time.
        if (
            not state or state.owner_open_id != owner or not message
            or message.create_time < int(state.activated_at.timestamp() * 1000)
        ):
            return None
        return dms_received_notice(
            payload, base_url=self.settings.app_base_url, chat_id=message.chat_id,
        )

    def _observe(self, db, owner, payload, *, baseline=False, notify_changes=True):
        if payload is None:
            return
        key = source_key(owner, payload["source_type"], payload["source_id"])
        source = db.get(StatusNoticeSource, key)
        is_message = payload["source_type"] == "message"
        submitted_result = is_message and payload.get("submitted_result") is True
        suppress = is_message and payload.get("suppress_notification") is True
        if suppress:
            if source:
                source.notify_changes = False
            db.execute(update(StatusNotification).where(
                StatusNotification.source_key == key,
                StatusNotification.status.in_(UNSENT),
                StatusNotification.submitted_at.is_(None),
            ).values(status="superseded", error_code=MESSAGE_EXPIRED, updated_at=utcnow()))
        if source and source.revision == payload["revision"]:
            return
        receipt = None
        if source is None:
            if not baseline and notify_changes:
                receipt = self._new_dms_receipt(db, owner, payload)
            source = StatusNoticeSource(
                key=key, owner_open_id=owner, source_type=payload["source_type"],
                source_id=payload["source_id"],
                # Discovery time is not message time. Backfilled mentions get a
                # durable silent baseline so re-syncs cannot revive old notices.
                notify_changes=notify_changes and not suppress and not (
                    is_message and payload.get("message_expired") is True and not submitted_result
                ),
                created_at=utcnow(),
            )
            db.add(source)
        source.revision, source.payload, source.updated_at = payload["revision"], payload, utcnow()
        db.execute(update(StatusNotification).where(
            StatusNotification.source_key == key,
            StatusNotification.status.in_(UNSENT),
            StatusNotification.submitted_at.is_(None),
            # Status changes may replace an unsent conclusion, but the initial
            # receipt remains a separate, durable step in the notification flow.
            func.coalesce(StatusNotification.source_revision, StatusNotification.revision)
            != dms_receipt_revision(source.source_id),
        ).values(status="superseded", updated_at=utcnow()))
        if baseline or not source.notify_changes and not submitted_result:
            return
        if receipt:
            db.add(StatusNotification(
                id=str(uuid4()), source_key=key, owner_open_id=owner,
                revision=receipt["revision"], phase=receipt["phase"], text=compact_text(receipt),
                source_revision=receipt["revision"],
                delivery_action="pending", card_payload=status_card(receipt),
                status="queued", created_at=utcnow(), updated_at=utcnow(),
            ))
        # Reobserving an old outcome after a retry/restart must not resend it.
        existing = db.scalar(select(StatusNotification).where(
            StatusNotification.source_key == key,
            or_(
                StatusNotification.source_revision == payload["revision"],
                StatusNotification.source_revision.is_(None)
                & (StatusNotification.revision == payload["revision"]),
            ),
        ).order_by(StatusNotification.created_at.desc(), StatusNotification.id.desc()).limit(1))
        delivery_revision = payload["revision"]
        if existing:
            if existing.delivery_action == "legacy" or (
                existing.status == "sent" and source.card_create_state == "ready"
                and source.card_sent_revision != payload["revision"]
            ):
                # A card reflects current state, so A -> B -> A needs another
                # PATCH. Keep prior delivery evidence (including legacy text)
                # and give this occurrence its own delivery identity.
                delivery_revision = hashlib.sha256(f'{payload["revision"]}:{uuid4()}'.encode()).hexdigest()
            elif (
                existing.submitted_at is None and existing.status == "superseded"
                and existing.error_code != MESSAGE_EXPIRED
                and existing.delivery_action != "legacy"
            ):
                existing.status, existing.error_code, existing.next_attempt_at = "queued", None, None
                existing.updated_at = utcnow()
                return
            else:
                return
        db.add(StatusNotification(
            id=str(uuid4()), source_key=key, owner_open_id=owner,
            revision=delivery_revision, source_revision=payload["revision"],
            phase=payload["phase"], text=compact_text(payload),
            delivery_action="pending", card_payload=status_card(payload),
            status="queued", created_at=utcnow(), updated_at=utcnow(),
        ))

    def _discover(self, db, state, *, baseline=False):
        owner = state.owner_open_id
        tracked = select(StatusNoticeSource.source_id).where(
            StatusNoticeSource.owner_open_id == owner,
            StatusNoticeSource.source_type == "approval",
        )
        for task in db.scalars(select(ApprovalTask).where(
            ApprovalTask.topic == "1",
            or_(ApprovalTask.visible.is_(True), ApprovalTask.task_id.in_(tracked)),
        )).all():
            self._observe(db, owner, self._approval(db, task, owner), baseline=baseline)
        if not baseline:
            rows = db.execute(select(Message, Chat).join(Chat, Chat.chat_id == Message.chat_id).where(
                Chat.visible.is_(True),
                Message.create_time >= int(state.activated_at.timestamp() * 1000),
                tuple_(Message.synced_at, Message.message_id)
                > tuple_(state.discovery_at, state.discovery_message_id),
            ).order_by(Message.synced_at, Message.message_id).limit(200)).all()
            for message, chat in rows:
                payload = self._dms_message(db, message, chat, owner) if message.chat_id == self.dms.chat_id else None
                self._observe(db, owner, payload or self._mention(db, message, chat, owner))
            if rows:
                state.discovery_at, state.discovery_message_id = rows[-1][0].synced_at, rows[-1][0].message_id
        tracked_attention = select(StatusNoticeSource.source_id).where(
            StatusNoticeSource.owner_open_id == owner,
            StatusNoticeSource.source_type.in_(["message", "dms_card"]),
        )
        for row in db.scalars(select(MessageAttention).where(
            MessageAttention.owner_open_id == owner,
            MessageAttention.source_type.in_(["message", "dms_card"]),
            or_(MessageAttention.created_at >= state.activated_at, MessageAttention.message_id.in_(tracked_attention), baseline),
        )).all():
            if row.source_type == "message":
                message = db.get(Message, row.message_id)
                chat = db.get(Chat, row.chat_id)
                if not message or message.create_time < int(state.activated_at.timestamp() * 1000):
                    continue
                payload = self._mention(db, message, chat, owner)
            else:
                payload = self._dms_notice(db, row, owner)
            self._observe(db, owner, payload, baseline=baseline)
        # A DMS card can be observed while the phone/model loop is still busy.
        # Continue refreshing those sources even before it creates its own row.
        for source in db.scalars(select(StatusNoticeSource).where(
            StatusNoticeSource.owner_open_id == owner,
            StatusNoticeSource.source_type == "dms_card",
        )).all():
            self._observe(db, owner, self._refresh(db, source, owner), baseline=baseline)

    def _tick(self):
        with self.sessions.begin() as db:
            owner = db.get(Account, 1)
            if not owner:
                return None
            state = db.get(StatusNotificationState, 1)
            if state is None:
                now = utcnow()
                state = StatusNotificationState(
                    id=1, owner_open_id=owner.open_id, activated_at=now,
                    discovery_at=now, discovery_message_id="",
                )
                db.add(state)
                self._discover(db, state, baseline=True)
                return {"status": "initialized", "sent": 0}
            if state.owner_open_id != owner.open_id:
                raise AppError("status_owner_changed", "即时私信绑定的账号不一致。", 409)
            # The advisory lock proves no other delivery is still executing.
            db.execute(update(StatusNotification).where(
                StatusNotification.status == "sending",
                StatusNotification.owner_open_id == owner.open_id,
            ).values(status="unknown", error_code="status_send_interrupted", updated_at=utcnow()))
            db.execute(update(StatusNoticeSource).where(
                StatusNoticeSource.owner_open_id == owner.open_id,
                StatusNoticeSource.card_create_state == "submitting",
            ).values(card_create_state="unknown"))
            # Old workers may have queued text during a rolling upgrade. Keep
            # its audit row but do not replay it or turn it into a new card.
            db.execute(update(StatusNotification).where(
                StatusNotification.owner_open_id == owner.open_id,
                StatusNotification.delivery_action == "legacy",
                StatusNotification.status.in_(UNSENT),
                StatusNotification.submitted_at.is_(None),
            ).values(status="superseded", error_code="status_legacy_migrated", updated_at=utcnow()))
            self._discover(db, state)
            ids = db.scalars(select(StatusNotification.id).where(
                StatusNotification.owner_open_id == owner.open_id,
                StatusNotification.status.in_(UNSENT),
                StatusNotification.submitted_at.is_(None),
                or_(StatusNotification.next_attempt_at.is_(None), StatusNotification.next_attempt_at <= utcnow()),
            ).order_by(
                StatusNotification.created_at,
                (StatusNotification.phase == "received").desc(),
                StatusNotification.id,
            ).limit(SEND_LIMIT)).all()
        sent = 0
        for identifier in ids:
            if self._deliver(identifier):
                sent += 1
        return {"status": "running", "sent": sent}

    def _deliver(self, identifier):
        try:
            permissions = self.feishu.self_message_permissions()
            if not isinstance(permissions, dict) or permissions.get("ready") is not True:
                raise AppError("self_message_permission_required", "缺少机器人发送权限。")
        except Exception as exc:
            with self.sessions.begin() as db:
                row = db.get(StatusNotification, identifier, with_for_update=True)
                if row and row.status in UNSENT and row.submitted_at is None:
                    row.status, row.error_code = "blocked_permission", _safe_error(exc)
                    row.next_attempt_at, row.updated_at = utcnow() + timedelta(seconds=60), utcnow()
            return False
        with self.sessions.begin() as db:
            row = db.get(StatusNotification, identifier, with_for_update=True)
            owner, state = db.get(Account, 1), db.get(StatusNotificationState, 1)
            if not row or row.status not in UNSENT or row.submitted_at:
                return False
            if not owner or not state or row.owner_open_id != owner.open_id or state.owner_open_id != owner.open_id:
                raise AppError("status_owner_changed", "即时私信绑定的账号不一致。", 409)
            source = db.get(StatusNoticeSource, row.source_key, with_for_update=True)
            latest = self._refresh(db, source, owner.open_id) if source else None
            if source and source.source_type == "message" and latest:
                submitted_result = latest.get("submitted_result") is True
                expired = not submitted_result and (
                    not source.notify_changes
                    or row.phase == "received" and latest.get("message_expired") is True
                    or row.created_at + timedelta(seconds=self.settings.attention_timeout_seconds) <= utcnow()
                )
                if latest.get("suppress_notification") is True or expired:
                    row.status, row.error_code, row.updated_at = "superseded", MESSAGE_EXPIRED, utcnow()
                    if latest.get("suppress_notification") is True:
                        self._observe(db, owner.open_id, latest)
                    return False
            is_receipt = bool(
                source and source.source_type == "dms_card"
                and _source_revision(row) == dms_receipt_revision(source.source_id)
            )
            if not latest or not is_receipt and (
                latest["revision"] != _source_revision(row) or source.revision != _source_revision(row)
            ):
                row.status, row.updated_at = "superseded", utcnow()
                self._observe(db, owner.open_id, latest)
                return False
            if source.source_type == "dms_card" and not is_receipt:
                receipt = db.scalar(select(StatusNotification).where(
                    StatusNotification.source_key == source.key,
                    func.coalesce(StatusNotification.source_revision, StatusNotification.revision)
                    == dms_receipt_revision(source.source_id),
                ))
                if receipt and receipt.submitted_at is None and receipt.status in UNSENT:
                    return False
            if is_receipt:
                # Revalidate the trusted source immediately before sending,
                # while allowing its current business state to have advanced.
                self._observe(db, owner.open_id, latest)
            if row.delivery_action == "legacy":
                row.status, row.error_code = "superseded", "status_legacy_migrated"
                row.updated_at = utcnow()
                return False
            if source.card_create_state in {"submitting", "unknown"}:
                row.status, row.error_code = "blocked_unknown", "status_card_create_unknown"
                row.updated_at = utcnow()
                return False
            if source.card_create_state == "ready":
                blocker = db.scalar(select(StatusNotification).where(
                    StatusNotification.source_key == source.key,
                    StatusNotification.delivery_action == "update",
                    StatusNotification.status.in_(["sending", "unknown", "failed"]),
                ).order_by(StatusNotification.created_at).limit(1))
                if blocker:
                    row.status = "failed" if blocker.status == "failed" else "blocked_unknown"
                    row.error_code = blocker.error_code if blocker.status == "failed" else "status_card_update_unknown"
                    row.updated_at = utcnow()
                    return False
                if not source.card_message_id or not source.card_receipt:
                    row.status, row.error_code = "failed", "status_card_unverified"
                    row.updated_at = utcnow()
                    return False
                action, target, receipt = "update", source.card_message_id, dict(source.card_receipt)
            elif source.card_create_state == "none" and not source.card_message_id and not source.card_receipt:
                action, target, receipt = "create", None, None
                source.card_create_state = "submitting"
            else:
                row.status, row.error_code = "failed", "status_card_unverified"
                row.updated_at = utcnow()
                return False
            payload = dms_received_notice(
                latest, base_url=self.settings.app_base_url, chat_id=self.dms.chat_id,
            ) if is_receipt else latest
            row.text, row.card_payload = compact_text(payload), status_card(payload)
            row.delivery_action, row.message_id = action, target
            row.status, row.submitted_at, row.updated_at = "sending", utcnow(), utcnow()
            row.error_code, row.next_attempt_at = None, None
            card, expected_owner = row.card_payload, row.owner_open_id
        try:
            if action == "create":
                result = self.feishu.send_self_status_card(card, identifier, expected_owner=expected_owner)
            else:
                result = self.feishu.update_self_status_card(
                    target, card, expected_owner=expected_owner, expected_receipt=receipt,
                )
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,124}", message_id):
                raise AppError("status_response_invalid", "状态卡片写入结果无法确认。")
            if action == "update" and message_id != target:
                raise AppError("status_response_invalid", "卡片更新标识与已保存锚点不一致。")
            if action == "create":
                receipt = result.get("receipt")
                if (
                    not isinstance(receipt, dict) or receipt.get("message_id") != message_id
                    or receipt.get("owner_open_id") != expected_owner
                    or receipt.get("sender_app_id") != self.settings.feishu_app_id
                    or receipt.get("msg_type") != "interactive"
                    or not isinstance(receipt.get("chat_id"), str)
                    or not re.fullmatch(r"oc_[A-Za-z0-9_-]{1,124}", receipt["chat_id"])
                    or not re.fullmatch(r"[0-9]{1,15}", str(receipt.get("create_time", "")))
                    or int(receipt["create_time"]) <= 0
                ):
                    raise AppError("status_response_invalid", "状态卡片的创建回执不完整。")
        except Exception as exc:
            self._failed_delivery(identifier, exc)
            return False
        # A database failure here must leave the submitted marker intact. The
        # next owner-locked tick recovers it as unknown, never a second create.
        self._complete_delivery(identifier, message_id, receipt)
        return True

    def _failed_delivery(self, identifier, exc):
        with self.sessions.begin() as db:
            row = db.get(StatusNotification, identifier, with_for_update=True)
            source = db.get(StatusNoticeSource, row.source_key, with_for_update=True)
            if getattr(exc, "write_attempted", None) is False:
                retryable = getattr(exc, "retryable", False) is True
                row.status = "queued" if retryable else "failed"
                row.submitted_at = None
                row.next_attempt_at = utcnow() + timedelta(seconds=60) if retryable else None
                if row.delivery_action == "create" and source.card_create_state == "submitting":
                    source.card_create_state = "none"
            else:
                row.status = "unknown"
                if row.delivery_action == "create":
                    source.card_create_state = "unknown"
            row.error_code, row.updated_at = _safe_error(exc), utcnow()

    def _complete_delivery(self, identifier, message_id, receipt):
        with self.sessions.begin() as db:
            row = db.get(StatusNotification, identifier, with_for_update=True)
            source = db.get(StatusNoticeSource, row.source_key, with_for_update=True)
            owner = db.get(Account, 1)
            if not owner or source.owner_open_id != owner.open_id or row.owner_open_id != owner.open_id:
                raise AppError("status_owner_changed", "即时私信绑定的账号不一致。", 409)
            if row.status != "sending" or row.submitted_at is None:
                raise AppError("status_delivery_changed", "状态卡片提交记录已变化。", 409)
            if row.delivery_action == "create":
                if source.card_create_state != "submitting" or source.card_message_id:
                    raise AppError("status_delivery_changed", "状态卡片锚点已变化。", 409)
                source.card_create_state, source.card_message_id = "ready", message_id
                source.card_receipt = dict(receipt)
            elif source.card_create_state != "ready" or source.card_message_id != message_id:
                raise AppError("status_delivery_changed", "状态卡片锚点已变化。", 409)
            source.card_sent_revision = _source_revision(row)
            row.status, row.message_id = "sent", message_id
            row.sent_at = row.updated_at = utcnow()
