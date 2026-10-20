"""Durable event hints. Personal data is always fetched using the bound user's token."""

import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.errors import AppError
from app.models import (
    Account,
    ApprovalTask,
    Chat,
    EventInbox,
    Message,
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

    def tick(self):
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
        with self.sessions.begin() as db:
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
                if chat_id:
                    chat = db.get(Chat, chat_id)
                    # Bot-visible chats may not belong to this user. Never copy their content.
                    if chat and chat.visible:
                        enqueue(db, f"messages:{chat_id}", changed=True)
                    enqueue(db, "chats", changed=True)
                if event.event_type.startswith("approval."):
                    for topic in ("1", "2", "17", "18"):
                        enqueue(db, f"approvals:{topic}", changed=True)
                    code = event.hints.get("instance_code")
                    if code and db.scalar(
                        select(ApprovalTask).where(ApprovalTask.instance_code == code).limit(1)
                    ):
                        enqueue(db, f"instance:{code}", changed=True)
                event.processed_at = utcnow()

    def run_one_job(self):
        with self.sessions() as db:
            job = db.scalar(
                select(SyncJob)
                .where(SyncJob.next_attempt_at <= utcnow())
                .order_by(SyncJob.next_attempt_at, SyncJob.resource)
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
                current.attempts += 1
                current.error_code = exc.code
                current.next_attempt_at = utcnow() + timedelta(
                    seconds=min(300, 5 * 2 ** min(current.attempts, 6))
                )
            worker_status(self.sessions, "sync", "error", exc.code, resource=job.resource)
            return
        with self.sessions.begin() as db:
            current = db.get(SyncJob, job.resource)
            if result["has_more"] or current.requested_at != job.requested_at:
                current.next_attempt_at = utcnow() + timedelta(seconds=1)
                current.attempts, current.error_code = 0, None
            else:
                db.delete(current)
            if job.resource == "chats":
                for chat_id in db.scalars(select(Chat.chat_id).where(Chat.visible)):
                    enqueue(db, f"messages:{chat_id}")
            if job.resource.startswith("approvals:"):
                topic = job.resource.split(":")[1]
                for code in db.scalars(
                    select(ApprovalTask.instance_code)
                    .where(ApprovalTask.visible, ApprovalTask.topic == topic)
                    .distinct()
                ):
                    enqueue(db, f"instance:{code}")
        worker_status(self.sessions, "sync", "idle")
