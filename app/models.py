from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    # The single-user app always writes id=1; a different identity cannot replace it.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    open_id: Mapped[str] = mapped_column(String(128), unique=True)
    tenant_key: Mapped[str] = mapped_column(String(128), default="")
    name: Mapped[str] = mapped_column(String(256))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(Text, default="")
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DeviceAuthorization(Base):
    __tablename__ = "device_authorizations"
    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_code: Mapped[str | None] = mapped_column(Text)
    exchanged_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    interval: Mapped[int] = mapped_column(Integer)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EventInbox(Base):
    __tablename__ = "event_inbox"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    hints: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    resource: Mapped[str] = mapped_column(String(300), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))


class WorkerState(Base):
    __tablename__ = "worker_states"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_activity_order", text("last_activity_time DESC NULLS LAST"), "chat_id"),
    )
    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    chat_mode: Mapped[str] = mapped_column(String(32))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    visible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    generation: Mapped[str] = mapped_column(String(36))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_activity_time: Mapped[int | None] = mapped_column(BigInteger)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_synced_at_message_id", "synced_at", "message_id"),)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(128))
    msg_type: Mapped[str] = mapped_column(String(32))
    sender_name: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    create_time: Mapped[int] = mapped_column(BigInteger, index=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalTask(Base):
    __tablename__ = "approval_tasks"
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(4), primary_key=True)
    instance_code: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    initiator_name: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    visible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    generation: Mapped[str] = mapped_column(String(36))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalInstance(Base):
    __tablename__ = "approval_instances"
    instance_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncState(Base):
    __tablename__ = "sync_states"
    resource: Mapped[str] = mapped_column(String(300), primary_key=True)
    page_token: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[str] = mapped_column(String(36))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    has_more: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resource: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16))
    items: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalReview(Base):
    __tablename__ = "approval_reviews"
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_code: Mapped[str] = mapped_column(String(128), index=True)
    owner_open_id: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32), default="needs_review")
    execution_status: Mapped[str] = mapped_column(String(32), default="awaiting_review")
    policy_version: Mapped[str] = mapped_column(String(128))
    rule_id: Mapped[str | None] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assessment_id: Mapped[str | None] = mapped_column(String(36))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    model_review: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ModelAssessment(Base):
    __tablename__ = "model_assessments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_code: Mapped[str] = mapped_column(String(128))
    owner_open_id: Mapped[str] = mapped_column(String(128))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    feedback_id: Mapped[str | None] = mapped_column(String(36))
    model: Mapped[str] = mapped_column(String(64))
    reasoning_effort: Mapped[str] = mapped_column(String(16))
    prompt_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="assessing")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalFeedback(Base):
    __tablename__ = "approval_feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    assessment_id: Mapped[str] = mapped_column(String(36))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    owner_open_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttentionState(Base):
    __tablename__ = "attention_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_open_id: Mapped[str] = mapped_column(String(128))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    discovery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    discovery_message_id: Mapped[str] = mapped_column(String(128), default="")
    last_call_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageAttention(Base):
    __tablename__ = "message_attention"
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(16), default="message", server_default="message")
    source_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    approval_task_id: Mapped[str | None] = mapped_column(String(128), index=True)
    approval_instance_code: Mapped[str | None] = mapped_column(String(128))
    approval_assessment_id: Mapped[str | None] = mapped_column(String(36))
    owner_open_id: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[str] = mapped_column(String(128))
    chat_name: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_hash: Mapped[str] = mapped_column(String(64))
    context_hash: Mapped[str | None] = mapped_column(String(64))
    assessment_context: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    model_result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    assessment_cache: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_uuid: Mapped[str | None] = mapped_column(String(36))
    reminder_message_id: Mapped[str | None] = mapped_column(String(128))
    reminder_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
