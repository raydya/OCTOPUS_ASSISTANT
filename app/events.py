"""Durable event hints. Personal data is always fetched using the bound user's token."""

import json
from datetime import timedelta

from sqlalchemy import case, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.errors import AppError
from app.models import (
    Account,
    ApprovalReview,
    ApprovalTask,
    Chat,
    EventInbox,
    Message,
    StatusNoticeSource,
    StatusNotification,
    SyncJob,
    WorkerState,
    utcnow,
)
from app.sync import upsert

EVENT_TYPES = (
    "im.message.receive_v1",
    "approval.instance.status_changed_v4",
    "approval.task.status_changed_v4",
)


def worker_status(sessions, name, status, error_code=None, **details):
    with sessions.begin() as db:
        upsert(
            db,
            WorkerState,
            {
                "name": name,
                "status": status,
                "error_code": error_code,
                "heartbeat_at": utcnow(),
                "details": details,
            },
        )


def enqueue(db, resource, *, changed=False):
    now = utcnow()
    statement = insert(SyncJob).values(
        resource=resource, requested_at=now, next_attempt_at=now, attempts=0
    )
    if changed:
        statement = statement.on_conflict_do_update(
            index_elements=["resource"], set_={"requested_at": now}
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=["resource"])
    db.execute(statement)


class EventInboxHandler:
    """SDK sends success only after this method's PostgreSQL transaction commits."""

    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    def _do_without_validation(self, payload: bytes):
        # SDK supplies decrypted, reassembled frames from its authenticated WSS connection.
        if len(payload) > 1024 * 1024:
            raise ValueError("event_too_large")
        try:
            envelope = json.loads(payload)
            header, event = envelope["header"], envelope["event"]
            event_type, event_id = header["event_type"], header["event_id"]
            if (
                not isinstance(event, dict)
                or not isinstance(event_id, str)
                or not 0 < len(event_id) <= 128
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid_event") from None
        if header.get("app_id") != self.settings.feishu_app_id or event_type not in EVENT_TYPES:
            return
        hints = {}
        if event_type == "im.message.receive_v1":
            value = (event.get("message") or {}).get("chat_id")
            if isinstance(value, str) and 0 < len(value) <= 128:
                hints["chat_id"] = value
        else:
            value = event.get("instance_code")
            if isinstance(value, str) and 0 < len(value) <= 128:
                hints["instance_code"] = value
        with self.sessions.begin() as db:
            # Bound database response time keeps the SDK acknowledgement prompt.
            from sqlalchemy import text

            db.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            account = db.get(Account, 1)
            if not account or header.get("tenant_key") != account.tenant_key:
                return
            db.execute(
                insert(EventInbox)
                .values(event_id=event_id, event_type=event_type, hints=hints, received_at=utcnow())
                .on_conflict_do_nothing(index_elements=["event_id"])
            )


class BackgroundSync:
    def __init__(self, settings, sessions, feishu, sync):
        self.settings, self.sessions, self.feishu, self.sync = settings, sessions, feishu, sync
        self.next_snapshot = utcnow()
        self.next_subscription = utcnow()

    @property
    def reminders_only(self):
        return getattr(self.settings, "reminders_only", True)

    @property
    def dms_configured(self):
        return bool(self.settings.dms_sender_app_id and self.settings.dms_chat_id)

    def reminder_instance_codes(self):
        """Read active tasks, uncertain submissions, and notified tasks awaiting closure."""
        owner = select(Account.open_id).where(Account.id == 1).scalar_subquery()
        pending = select(ApprovalTask.instance_code).where(
            ApprovalTask.topic == "1", ApprovalTask.visible, ApprovalTask.status == "1",
            ApprovalTask.raw["user_id"].as_string() == owner,
            # External workflows have no native detail endpoint. Their personal
            # handled-result lookup is needed only after a notified task leaves
            # pending, through the narrowly scoped completion branch below.
            func.coalesce(ApprovalTask.raw["instance_external_id"].as_string(), "") == "",
            func.coalesce(ApprovalTask.raw["task_external_id"].as_string(), "") == "",
        )
        submitted = select(ApprovalReview.instance_code).where(
            ApprovalReview.owner_open_id == owner,
            ApprovalReview.submitted_at.is_not(None),
            ApprovalReview.confirmed_at.is_(None),
            ApprovalReview.execution_status.in_(["submitting", "unknown", "approved"]),
            select(ApprovalTask.task_id).where(
                ApprovalTask.task_id == ApprovalReview.task_id,
                ApprovalTask.instance_code == ApprovalReview.instance_code,
            ).exists(),
        )
        # Leaving the pending list is not a completion result. Finish reading
        # only the user's already-delivered cards while they remain updatable;
        # this never scans the historical approval lists or legacy text notices.
        recent_card = select(StatusNotification.id).where(
            StatusNotification.source_key == StatusNoticeSource.key,
            StatusNotification.owner_open_id == owner,
            StatusNotification.message_id == StatusNoticeSource.card_message_id,
            StatusNotification.delivery_action == "create",
            StatusNotification.status == "sent",
            StatusNotification.sent_at > utcnow() - timedelta(days=14),
            StatusNotification.sent_at <= utcnow(),
        ).exists()
        completion = select(ApprovalTask.instance_code).join(
            StatusNoticeSource, StatusNoticeSource.source_id == ApprovalTask.task_id,
        ).where(
            ApprovalTask.topic == "1",
            or_(ApprovalTask.visible.is_(False), ApprovalTask.status != "1"),
            ApprovalTask.raw["user_id"].as_string() == owner,
            ApprovalTask.raw["task_id"].as_string() == ApprovalTask.task_id,
            ApprovalTask.raw["instance_code"].as_string() == ApprovalTask.instance_code,
            StatusNoticeSource.owner_open_id == owner,
            StatusNoticeSource.source_type == "approval",
            StatusNoticeSource.card_create_state == "ready",
            StatusNoticeSource.card_message_id.is_not(None),
            StatusNoticeSource.payload["phase"].as_string() == "responded",
            recent_card,
        )
        return pending.union(submitted, completion)

    def reminder_job_filter(self):
        instances = self.reminder_instance_codes().subquery()
        resources = ["approvals:1", *(["dms_cards"] if self.dms_configured else [])]
        return or_(
            SyncJob.resource.in_(resources),
            SyncJob.resource.in_(select(literal("instance:") + instances.c.instance_code)),
        )

    def cancel_disallowed_jobs(self):
        if self.reminders_only:
            with self.sessions.begin() as db:
                # Retain all cached messages and approvals; only cancel work
                # that is outside the owner's reminder-only collection scope.
                db.execute(delete(SyncJob).where(~self.reminder_job_filter()))

    def tick(self):
        self.cancel_disallowed_jobs()
        with self.sessions() as db:
            if not db.get(Account, 1):
                worker_status(self.sessions, "sync", "waiting_authorization")
                return
        if self.next_subscription <= utcnow():
            try:
                self.feishu.subscribe_approvals()
                worker_status(
                    self.sessions, "subscriptions", "subscribed", event_types=list(EVENT_TYPES[1:])
                )
                self.next_subscription = utcnow() + timedelta(hours=1)
            except AppError as exc:
                worker_status(self.sessions, "subscriptions", "error", exc.code)
                self.next_subscription = utcnow() + timedelta(seconds=60)
        if self.next_snapshot <= utcnow():
            self.schedule_snapshot()
            self.next_snapshot = utcnow() + timedelta(seconds=self.settings.sync_interval_seconds)
        self.drain_events()
        self.run_one_job()

    def schedule_snapshot(self):
        self.cancel_disallowed_jobs()
        with self.sessions.begin() as db:
            if self.reminders_only:
                enqueue(db, "approvals:1")
                if self.dms_configured:
                    enqueue(db, "dms_cards")
                for code in db.scalars(self.reminder_instance_codes()):
                    enqueue(db, f"instance:{code}")
            else:
                enqueue(db, "chats")
                for topic in ("1", "2", "17", "18"):
                    enqueue(db, f"approvals:{topic}")
                for chat_id in db.scalars(select(Chat.chat_id).where(Chat.visible)):
                    enqueue(db, f"messages:{chat_id}")
                for thread_id in db.scalars(
                    select(Message.thread_id)
                    .join(Chat, Message.chat_id == Chat.chat_id)
                    .where(Chat.visible, Message.thread_id.is_not(None))
                    .distinct()
                ):
                    enqueue(db, f"thread:{thread_id}")
            # Retain only event IDs and resource hints, never event message bodies.
            db.execute(
                delete(EventInbox).where(EventInbox.processed_at < utcnow() - timedelta(days=7))
            )

    def drain_events(self):
        with self.sessions.begin() as db:
            events = db.scalars(
                select(EventInbox)
                .where(EventInbox.processed_at.is_(None))
                .order_by(EventInbox.received_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for event in events:
                chat_id = event.hints.get("chat_id")
                if self.reminders_only:
                    # Bot-visible ordinary chats are not a reason to collect
                    # conversations. Only the configured DBA source is relevant.
                    if chat_id and self.dms_configured and chat_id == self.settings.dms_chat_id:
                        enqueue(db, "dms_cards", changed=True)
                    event.hints = {
                        key: value for key, value in event.hints.items()
                        if key not in {"chat_lookup_pending", "chat_lookup_retried"}
                    }
                elif chat_id:
                    chat = db.get(Chat, chat_id)
                    # Bot-visible chats may not belong to this user. Never copy their content.
                    if chat and chat.visible:
                        enqueue(db, f"messages:{chat_id}", changed=True)
                    else:
                        # The current list cursor may predate this chat. Retain a bounded
                        # lookup hint after acknowledging the event, without importing it.
                        event.hints = {**event.hints, "chat_lookup_pending": True}
                    enqueue(db, "chats", changed=True)
                if event.event_type.startswith("approval."):
                    for topic in (("1",) if self.reminders_only else ("1", "2", "17", "18")):
                        enqueue(db, f"approvals:{topic}", changed=True)
                    code = event.hints.get("instance_code")
                    known = (
                        code in set(db.scalars(self.reminder_instance_codes()))
                        if code and self.reminders_only else bool(code and db.scalar(
                            select(ApprovalTask).where(ApprovalTask.instance_code == code).limit(1)
                        ))
                    )
                    if known:
                        enqueue(db, f"instance:{code}", changed=True)
                event.processed_at = utcnow()

    def finish_chat_lookups(self, db):
        if self.reminders_only:
            return False
        retry = False
        for event in db.scalars(
            select(EventInbox).where(
                EventInbox.event_type == "im.message.receive_v1",
                EventInbox.received_at >= utcnow() - timedelta(minutes=30),
                EventInbox.hints["chat_lookup_pending"].as_boolean().is_(True),
            )
        ):
            hints = dict(event.hints)
            chat_id = hints["chat_id"]
            chat = db.get(Chat, chat_id)
            if chat and chat.visible:
                enqueue(db, f"messages:{chat_id}", changed=True)
                hints.pop("chat_lookup_pending")
            elif not hints.get("chat_lookup_retried"):
                # One fresh pass after the old cursor finishes catches newly created
                # chats. Bot-only/inaccessible chats cannot cause an endless rescan.
                hints["chat_lookup_retried"] = True
                retry = True
            else:
                hints.pop("chat_lookup_pending")
            event.hints = hints
        return retry

    def run_one_job(self):
        self.cancel_disallowed_jobs()
        recent_message = (
            select(EventInbox.event_id)
            .where(
                EventInbox.event_type == "im.message.receive_v1",
                EventInbox.received_at >= utcnow() - timedelta(minutes=30),
                SyncJob.resource == "messages:" + EventInbox.hints["chat_id"].as_string(),
            )
            .exists()
        )
        pending_instance = (
            select(ApprovalTask.task_id)
            .where(
                ApprovalTask.visible,
                ApprovalTask.topic == "1",
                SyncJob.resource == "instance:" + ApprovalTask.instance_code,
            )
            .exists()
        )
        priority = case(
            (SyncJob.resource == "dms_cards", 0),
            (
                SyncJob.resource == f"messages:{getattr(self.settings, 'dms_chat_id', '')}"
                if getattr(self.settings, "dms_sender_app_id", "") else False,
                0,
            ),
            (
                SyncJob.resource.in_(
                    ["chats", "approvals:1", "approvals:2", "approvals:17", "approvals:18"]
                ),
                0,
            ),
            (recent_message, 1),
            (pending_instance, 2),
            else_=3,
        )
        with self.sessions() as db:
            job = db.scalar(
                select(SyncJob)
                .where(
                    SyncJob.next_attempt_at <= utcnow(),
                    self.reminder_job_filter() if self.reminders_only else True,
                )
                .order_by(priority, SyncJob.next_attempt_at, SyncJob.resource)
                .limit(1)
            )
            if not job:
                worker_status(self.sessions, "sync", "idle")
                return
        try:
            worker_status(self.sessions, "sync", "syncing", resource=job.resource)
            result = self.sync.run(job.resource)
        except AppError as exc:
            with self.sessions.begin() as db:
                current = db.get(SyncJob, job.resource)
                if current:
                    current.attempts += 1
                    current.error_code = exc.code
                    current.next_attempt_at = utcnow() + timedelta(
                        seconds=min(300, 5 * 2 ** min(current.attempts, 6))
                    )
            worker_status(self.sessions, "sync", "error", exc.code, resource=job.resource)
            return
        with self.sessions.begin() as db:
            current = db.get(SyncJob, job.resource)
            if not current:
                return
            retry_chat_lookup = (
                job.resource == "chats" and not result["has_more"] and self.finish_chat_lookups(db)
            )
            if result["has_more"] or current.requested_at != job.requested_at or retry_chat_lookup:
                current.next_attempt_at = utcnow() + timedelta(seconds=1)
                current.attempts, current.error_code = 0, None
            else:
                db.delete(current)
            if job.resource == "chats" and not self.reminders_only:
                for chat_id in db.scalars(select(Chat.chat_id).where(Chat.visible)):
                    enqueue(db, f"messages:{chat_id}")
            if job.resource.startswith("approvals:"):
                topic = job.resource.split(":")[1]
                codes = self.reminder_instance_codes() if self.reminders_only else (
                    select(ApprovalTask.instance_code)
                    .where(ApprovalTask.visible, ApprovalTask.topic == topic)
                    .distinct()
                )
                for code in db.scalars(codes):
                    enqueue(db, f"instance:{code}")
        worker_status(self.sessions, "sync", "idle")
