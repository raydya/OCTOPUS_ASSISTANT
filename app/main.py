import asyncio
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.approval_review import _eligibility_errors, policy_summary, review_view
from app.attention import AttentionService, attention_view
from app.config import Settings
from app.db import DatabaseNotReady, check_database, make_database, wait_for_database
from app.device_auth import DeviceAuthService
from app.errors import AppError
from app.feishu import FeishuClient
from app.model_review import ModelReviewService, business_snapshot
from app.models import (
    Account,
    ApprovalInstance,
    ApprovalReview,
    ApprovalTask,
    BrowserSession,
    Chat,
    DeviceAuthorization,
    EventInbox,
    Message,
    MessageAttention,
    StatusNotification,
    SyncJob,
    SyncRun,
    SyncState,
    WorkerState,
    utcnow,
)
from app.security import COOKIE_NAME, Security
from app.status_notifications import notification_view
from app.sync import SyncService, instance_view

STATIC = Path(__file__).parent / "static"
Topic = Literal["1", "2", "17", "18"]


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=512, repr=False)


class SyncBody(BaseModel):
    days: int = Field(default=7, ge=1, le=365)
    restart: bool = False


class ApprovalFeedbackBody(BaseModel):
    assessment_id: str = Field(min_length=1, max_length=36)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["answer", "approve", "hold"]
    text: str = Field(default="", max_length=8000)


def create_app(settings=None, *, transport=None):
    settings = settings or Settings()
    engine, sessions = make_database(settings)
    security = Security(settings, sessions)
    feishu = FeishuClient(settings, security, sessions, transport=transport)
    sync = SyncService(settings, sessions, feishu)
    device_auth = DeviceAuthService(sessions, security, feishu)
    model_review = ModelReviewService(settings, sessions, provider=None)
    attention = AttentionService(settings, sessions, feishu, provider=None)

    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(
                wait_for_database, engine, timeout=settings.database_wait_seconds
            )
            yield
        finally:
            feishu.close()
            engine.dispose()

    app = FastAPI(
        title="Octopus Assistant",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.sessions = sessions
    app.state.feishu = feishu
    app.state.sync = sync
    app.state.security = security
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(settings.app_base_url).hostname, "localhost", "127.0.0.1"],
    )

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"error": {"code": "invalid_input", "message": "请求参数不合法，请检查后重试。"}},
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "database_unavailable",
                    "message": "数据库暂时不可用，请检查 PostgreSQL 与数据库迁移。",
                }
            },
            status_code=503,
        )

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != settings.app_base_url:
            raise AppError("invalid_origin", "请求来源不匹配，请从配置的工作台地址访问。", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise AppError("invalid_origin", "不接受跨站请求。", 403)

    def authenticated(request: Request):
        return security.session(request.cookies.get(COOKIE_NAME))

    def mutation(request: Request, session=Depends(authenticated)):
        same_origin(request)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf_token):
            raise AppError("invalid_csrf", "页面已失效，请刷新工作台后重试。", 403)
        return session

    login_failures = defaultdict(list)
    login_lock = threading.Lock()

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz")
    def health():
        try:
            check_database(engine)
        except (SQLAlchemyError, DatabaseNotReady):
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request, _=Depends(same_origin)):
        ip = request.client.host if request.client else "local"
        with login_lock:
            now = time.monotonic()
            login_failures[ip] = [t for t in login_failures[ip] if t > now - 60]
            if len(login_failures[ip]) >= 10:
                raise AppError("rate_limited", "尝试次数过多，请一分钟后重试。", 429)
            login_failures[ip].append(now)
        token = security.login(body.password)
        with login_lock:
            login_failures.pop(ip, None)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="lax",
            max_age=settings.session_hours * 3600,
            path="/",
        )
        return response

    @app.post("/auth/logout")
    def logout(session=Depends(mutation)):
        with sessions.begin() as db:
            db.execute(
                delete(BrowserSession).where(BrowserSession.token_hash == session.token_hash)
            )
            db.execute(
                delete(DeviceAuthorization).where(
                    DeviceAuthorization.session_hash == session.token_hash
                )
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.post("/auth/feishu/start")
    def start(session=Depends(mutation)):
        if not settings.feishu_configured:
            raise AppError(
                "not_configured", "请先在 .env 填写飞书 App ID 和 App Secret，并重启应用。", 409
            )
        return device_auth.start(session.token_hash)

    @app.post("/auth/feishu/poll")
    def poll(session=Depends(mutation)):
        return device_auth.poll(session.token_hash)

    @app.get("/api/status")
    def status(session=Depends(authenticated)):
        with sessions() as db:
            account = db.get(Account, 1)

            def count(model, *where):
                return db.scalar(select(func.count()).select_from(model).where(*where))

            return {
                "csrf_token": session.csrf_token,
                "configured": settings.feishu_configured,
                "connection_mode": "websocket",
                "authorization_mode": "device",
                "sync_interval_seconds": settings.sync_interval_seconds,
                "reminders_only": settings.reminders_only,
                "workers": [
                    {
                        "name": w.name,
                        "status": w.status,
                        "error_code": w.error_code,
                        "heartbeat_at": w.heartbeat_at,
                        "details": w.details,
                        # Subscription registration is durable, not a periodic heartbeat.
                        "stale": (
                            w.name in {"events", "status_notifications"}
                            and (utcnow() - w.heartbeat_at).total_seconds() > 60
                        ) or (
                            w.name in {"approval_model", "approval_review", "attention"}
                            and (utcnow() - w.heartbeat_at).total_seconds()
                            > settings.approval_model_timeout_seconds + 120
                        ) or (
                            w.name == "mentions"
                            and (utcnow() - w.heartbeat_at).total_seconds()
                            > max(60, settings.sync_interval_seconds) + 120
                        ),
                    }
                    for w in db.scalars(select(WorkerState))
                ],
                "event_count": count(EventInbox),
                "queued_syncs": count(SyncJob),
                "status_notifications": {
                    "enabled": settings.status_notifications_enabled,
                    "sent": count(StatusNotification, StatusNotification.status == "sent", StatusNotification.owner_open_id == (account.open_id if account else "")),
                    "pending": count(StatusNotification, StatusNotification.status.in_(["queued", "blocked_permission"]), StatusNotification.owner_open_id == (account.open_id if account else "")),
                    "unknown": count(StatusNotification, StatusNotification.status == "unknown", StatusNotification.owner_open_id == (account.open_id if account else "")),
                },
                "approval_automation": {
                    "enabled": settings.approval_automation_enabled,
                    **policy_summary(settings),
                    "write_scope_granted": bool(
                        account and "approval:task:write" in account.scope.split()
                    ),
                    "needs_review": db.scalar(
                        select(func.count()).select_from(ApprovalReview).join(
                            ApprovalTask, ApprovalTask.task_id == ApprovalReview.task_id
                        ).where(
                            ApprovalReview.decision == "needs_review",
                            ApprovalTask.visible, ApprovalTask.topic == "1",
                        )
                    ),
                    "model": {
                        "enabled": settings.approval_model_enabled,
                        "model": settings.approval_model,
                        "reasoning_effort": settings.approval_model_reasoning_effort,
                    },
                },
                "requested_scopes": settings.feishu_scopes.split(),
                "attention": {
                    "enabled": settings.attention_enabled,
                    "timeout_seconds": settings.attention_timeout_seconds,
                    "call_cooldown_seconds": settings.attention_call_cooldown_seconds,
                    "quiet_hours": False,
                    "approval_intervention_enabled": settings.attention_enabled,
                    "dms_card_supported": attention.dms.configured,
                    "model": settings.approval_model,
                    "reasoning_effort": settings.approval_model_reasoning_effort,
                    "buzz_supported": False,
                    "required_bot_scopes": [
                        "im:message:send_as_bot", "im:message.urgent:phone",
                    ],
                },
                "account": {
                    "name": account.name,
                    "open_id": account.open_id,
                    "scopes": account.scope.split(),
                    "expires_at": account.expires_at,
                    "can_refresh": bool(account.refresh_token),
                }
                if account
                else None,
                "counts": {
                    "chats": count(Chat, Chat.visible),
                    "messages": count(Message),
                    "pending": count(ApprovalTask, ApprovalTask.visible, ApprovalTask.topic == "1"),
                },
                "sync_states": [
                    {
                        "resource": s.resource,
                        "has_more": s.has_more,
                        "parameters": s.parameters,
                        "synced_at": s.synced_at,
                        "completed_at": s.completed_at,
                    }
                    for s in db.scalars(select(SyncState))
                ],
            }

    def page(rows, limit, mapper):
        return {"items": [mapper(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @app.get("/api/chats", dependencies=[Depends(authenticated)])
    def chats(
        q: str = Query("", max_length=200),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        with sessions() as db:
            query = select(Chat).where(Chat.visible)
            if q:
                query = query.where(Chat.name.icontains(q, autoescape=True))
            rows = list(
                db.scalars(
                    query.order_by(Chat.last_activity_time.desc().nulls_last(), Chat.chat_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return page(
                rows,
                limit,
                lambda c: {
                    "chat_id": c.chat_id,
                    "name": c.name,
                    "chat_mode": c.chat_mode,
                    "synced_at": c.synced_at,
                    "last_activity_time": c.last_activity_time,
                },
            )

    @app.get("/api/chats/{chat_id}/messages", dependencies=[Depends(authenticated)])
    def messages(
        chat_id: str,
        thread_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        with sessions() as db:
            query = select(Message).where(Message.chat_id == chat_id)
            if thread_id:
                query = query.where(Message.thread_id == thread_id)
            rows = list(
                db.scalars(
                    query.order_by(Message.create_time.desc(), Message.message_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return page(
                rows,
                limit,
                lambda m: {
                    "message_id": m.message_id,
                    "text": m.text,
                    "sender_name": m.sender_name,
                    "msg_type": m.msg_type,
                    "create_time": m.create_time,
                    "thread_id": m.thread_id,
                    "deleted": m.deleted,
                    "raw": m.raw,
                    "synced_at": m.synced_at,
                },
            )

    @app.get("/api/approvals", dependencies=[Depends(authenticated)])
    def approvals(
        topic: Topic = "1", limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        with sessions() as db:
            rows = list(
                db.scalars(
                    select(ApprovalTask)
                    .where(ApprovalTask.visible, ApprovalTask.topic == topic)
                    .order_by(ApprovalTask.title, ApprovalTask.task_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return page(
                rows,
                limit,
                lambda t: {
                    "task_id": t.task_id,
                    "instance_code": t.instance_code,
                    "title": t.title,
                    "status": t.status,
                    "initiator_name": t.initiator_name,
                    "summaries": t.raw.get("summaries", []),
                    "review": review_view(db.get(ApprovalReview, t.task_id)),
                    "synced_at": t.synced_at,
                },
            )

    @app.get("/api/approval-reviews", dependencies=[Depends(authenticated)])
    def approval_reviews(
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        with sessions() as db:
            rows = list(
                db.scalars(
                    select(ApprovalReview)
                    .order_by(ApprovalReview.reviewed_at.desc(), ApprovalReview.task_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return page(rows, limit, review_view)

    @app.get("/api/approval-reviews/{task_id}", dependencies=[Depends(authenticated)])
    def approval_review(task_id: str):
        with sessions() as db:
            return {"data": review_view(db.get(ApprovalReview, task_id))}

    @app.post("/api/approval-reviews/{task_id}/respond", dependencies=[Depends(mutation)])
    def approval_respond(task_id: str, body: ApprovalFeedbackBody):
        if not settings.approval_automation_enabled or not settings.approval_model_enabled:
            raise AppError("approval_model_disabled", "模型审批已暂停，不能提交新的处理指示。", 409)
        with sessions() as db:
            review = db.get(ApprovalReview, task_id)
            account = db.get(Account, 1)
            if not review or not account or review.owner_open_id != account.open_id:
                raise AppError("approval_feedback_stale", "该审批记录不可用。", 409)
            instance_code, owner = review.instance_code, account.open_id
        # A response grants authority only to a fresh, exact business snapshot.
        listing = feishu.tasks("1", None)
        rows = listing.get("tasks") if isinstance(listing, dict) else None
        matches = [
            task for task in rows
            if isinstance(task, dict) and task.get("task_id") == task_id
        ] if isinstance(rows, list) else []
        detail = feishu.instance(instance_code)
        if (
            len(matches) != 1 or not isinstance(detail, dict)
            or _eligibility_errors(matches[0], detail, task_id, instance_code, owner)
        ):
            raise AppError(
                "approval_feedback_stale", "最新待办或处理资格已变化，请刷新后查看。", 409
            )
        return {"data": model_review.respond(
            task_id, body.assessment_id, body.snapshot_hash,
            body.action, text=body.text,
            current_snapshot=business_snapshot(matches[0], detail, owner),
        )}

    @app.get("/api/approvals/{instance_code}", dependencies=[Depends(authenticated)])
    def instance(instance_code: str):
        with sessions() as db:
            record = db.get(ApprovalInstance, instance_code)
            if not record:
                return {"data": None}
            return instance_view(record)

    @app.get("/api/sync-runs", dependencies=[Depends(authenticated)])
    def runs():
        with sessions() as db:
            return {
                "items": [
                    {
                        "resource": r.resource,
                        "status": r.status,
                        "items": r.items,
                        "error_code": r.error_code,
                        "finished_at": r.finished_at,
                    }
                    for r in db.scalars(
                        select(SyncRun).order_by(SyncRun.finished_at.desc()).limit(30)
                    )
                ]
            }

    @app.get("/api/status-notifications", dependencies=[Depends(authenticated)])
    def status_notifications(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with sessions() as db:
            owner = db.get(Account, 1)
            if not owner:
                return {"items": [], "has_more": False}
            rows = db.scalars(select(StatusNotification).where(
                StatusNotification.owner_open_id == owner.open_id,
            ).order_by(StatusNotification.created_at.desc(), StatusNotification.id).offset(offset).limit(limit + 1)).all()
            return page(rows, limit, notification_view)

    @app.get("/api/attention", dependencies=[Depends(authenticated)])
    def attention_records(
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    ):
        with sessions() as db:
            rows = list(db.scalars(
                select(MessageAttention)
                .order_by(MessageAttention.updated_at.desc(), MessageAttention.message_id)
                .offset(offset).limit(limit + 1)
            ))
            return page(rows, limit, attention_view)

    @app.post("/api/attention/{message_id}/ack", dependencies=[Depends(mutation)])
    def attention_ack(message_id: str):
        return {"data": attention.acknowledge(message_id)}

    @app.post("/api/sync/chats", dependencies=[Depends(mutation)])
    def sync_chats(body: SyncBody):
        if settings.reminders_only:
            raise AppError("message_sync_disabled", "普通与历史消息同步已停用，仅读取提醒所需信息。", 403)
        return sync.run("chats", restart=body.restart)

    @app.post("/api/sync/messages/{chat_id}", dependencies=[Depends(mutation)])
    def sync_messages(chat_id: str, body: SyncBody):
        if settings.reminders_only:
            raise AppError("message_sync_disabled", "普通与历史消息同步已停用，仅读取提醒所需信息。", 403)
        return sync.run(f"messages:{chat_id}", days=body.days, restart=body.restart)

    @app.post("/api/sync/threads/{thread_id}", dependencies=[Depends(mutation)])
    def sync_thread(thread_id: str, body: SyncBody):
        if settings.reminders_only:
            raise AppError("message_sync_disabled", "普通与历史消息同步已停用，仅读取提醒所需信息。", 403)
        return sync.run(f"thread:{thread_id}", restart=body.restart)

    @app.post("/api/sync/approvals/{topic}", dependencies=[Depends(mutation)])
    def sync_approvals(topic: Topic, body: SyncBody):
        return sync.run(f"approvals:{topic}", restart=body.restart)

    @app.post("/api/sync/instances/{instance_code}", dependencies=[Depends(mutation)])
    def sync_instance(instance_code: str):
        return sync.run(f"instance:{instance_code}")

    @app.get("/api/openapi.json", dependencies=[Depends(authenticated)], include_in_schema=False)
    def schema():
        return app.openapi()

    return app
