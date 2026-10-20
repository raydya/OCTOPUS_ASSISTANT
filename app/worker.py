"""One outbound Feishu WebSocket plus a durable personal-data synchronization worker."""

import asyncio
import contextlib
import signal
import sys
import time
from urllib.parse import urlsplit

from lark_oapi.core.enum import LogLevel
from lark_oapi.ws.client import Client, loop
from lark_oapi.ws.model import ClientConfig
from sqlalchemy import text, update

from app.approval_review import ApprovalReviewer
from app.config import Settings
from app.db import check_database, make_database, wait_for_database
from app.errors import AppError, FeishuError
from app.events import BackgroundSync, EventInboxHandler, worker_status
from app.feishu import FeishuClient
from app.models import WorkerState, utcnow
from app.security import Security
from app.sync import SyncService


class FeishuSocket(Client):
    """Small SDK adapter: bounded discovery, quiet logs and lifecycle supervision.

    The SDK owns framing, multipart assembly, acknowledgements and heartbeats.
    Internal hooks are pinned to lark-oapi 1.7.5 and exercised by protocol tests.
    """

    def __init__(self, settings, feishu, inbox):
        super().__init__(
            settings.feishu_app_id,
            settings.feishu_app_secret.get_secret_value(),
            event_handler=inbox,
            auto_reconnect=False,
            log_level=LogLevel.CRITICAL,
        )
        self.feishu = feishu
        self.last_pong = time.monotonic()

    def _get_conn_url(self):
        body = self.feishu._request(
            "POST",
            "https://open.feishu.cn/callback/ws/endpoint",
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
        )
        data = body.get("data") or {}
        url = data.get("URL", "")
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "wss" or not host.endswith((".feishu.cn", ".larksuite.com")):
            raise FeishuError("invalid_ws_endpoint")
        if data.get("ClientConfig"):
            self._configure(ClientConfig(data["ClientConfig"]))
        self.last_pong = time.monotonic()
        return url

    async def _handle_control_frame(self, frame):
        await super()._handle_control_frame(frame)
        if any(h.key == "type" and h.value == "pong" for h in frame.headers):
            self.last_pong = time.monotonic()

    async def _receive_message_loop(self):
        # Supervisor below reconnects; do not leave unobserved exception tasks.
        with contextlib.suppress(Exception):
            await super()._receive_message_loop()


async def run(settings):
    engine, sessions = make_database(settings)
    try:
        await asyncio.to_thread(wait_for_database, engine, timeout=settings.database_wait_seconds)
    except Exception:
        engine.dispose()
        raise
    security = Security(settings, sessions)
    feishu = FeishuClient(settings, security, sessions)
    ws_http = FeishuClient(settings, security, sessions)
    approval_http = FeishuClient(settings, security, sessions)
    reviewer = ApprovalReviewer(settings, sessions, approval_http)
    sync = BackgroundSync(settings, sessions, feishu, SyncService(settings, sessions, feishu))
    socket = FeishuSocket(settings, ws_http, EventInboxHandler(settings, sessions))
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    # Session-level PG lock prevents a second replica splitting event delivery.
    leader = engine.connect()
    if not leader.scalar(text("SELECT pg_try_advisory_lock(81024003)")):
        leader.close()
        raise RuntimeError("worker_already_running")
    leader.commit()

    async def reconcile():
        while not stopping.is_set():
            try:
                await asyncio.to_thread(sync.tick)
            except Exception:
                # No exception repr: SDK/DB exceptions can include private parameters.
                worker_status(sessions, "sync", "error", "worker_sync_error")
            await asyncio.sleep(1)

    async def review_approvals():
        while not stopping.is_set():
            try:
                if settings.approval_automation_enabled:
                    await asyncio.to_thread(reviewer.tick)
                    worker_status(sessions, "approval_review", "running")
                else:
                    worker_status(sessions, "approval_review", "disabled")
            except Exception:
                # Keep failures isolated from event ingestion and personal sync.
                worker_status(sessions, "approval_review", "error", "approval_review_error")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=5)
            except TimeoutError:
                pass

    reconcile_task = asyncio.create_task(reconcile())
    approval_task = asyncio.create_task(review_approvals())
    ping_task = asyncio.create_task(socket._ping_loop())
    next_connect, retry_delay, heartbeat = 0, 2, 0
    try:
        while not stopping.is_set():
            now = time.monotonic()
            if now >= heartbeat:
                # A lost leadership connection is fatal; never silently run two receivers.
                leader.execute(text("SELECT 1"))
                leader.commit()
                if socket._conn is not None:
                    worker_status(sessions, "events", "connected")
                else:
                    with sessions.begin() as db:
                        db.execute(
                            update(WorkerState)
                            .where(WorkerState.name == "events")
                            .values(heartbeat_at=utcnow())
                        )
                heartbeat = now + 10
            if socket._conn is None and now >= next_connect:
                try:
                    if not settings.feishu_configured:
                        worker_status(sessions, "events", "not_configured")
                        next_connect = now + 15
                    elif await asyncio.to_thread(ws_http.online_connections):
                        worker_status(sessions, "events", "remote_connection_busy")
                        next_connect = now + 30
                    else:
                        worker_status(sessions, "events", "connecting")
                        await socket._connect()
                        worker_status(sessions, "events", "connected")
                        retry_delay = 2
                except Exception as exc:
                    worker_status(
                        sessions,
                        "events",
                        "reconnecting",
                        exc.code if isinstance(exc, AppError) else "ws_connect_failed",
                    )
                    next_connect = time.monotonic() + retry_delay
                    retry_delay = min(60, retry_delay * 2)
            if socket._conn is not None and now - socket.last_pong > socket._ping_interval * 2 + 30:
                await socket._disconnect()
                worker_status(sessions, "events", "reconnecting", "heartbeat_timeout")
                next_connect = time.monotonic() + 2
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        stopping.set()
        ping_task.cancel()
        # Let in-flight API/DB work finish before disposing shared resources.
        await reconcile_task
        await approval_task
        await socket._disconnect()
        remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        worker_status(sessions, "events", "stopped")
        leader.close()
        feishu.close()
        ws_http.close()
        approval_http.close()
        engine.dispose()


def check_health():
    engine, sessions = make_database(Settings())
    try:
        check_database(engine)
        with sessions() as db:
            state = db.get(WorkerState, "events")
            if (
                not state
                or state.status == "stopped"
                or (utcnow() - state.heartbeat_at).total_seconds() > 45
            ):
                raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        check_health()
    else:
        try:
            loop.run_until_complete(run(Settings()))
        except Exception:
            print(
                "worker stopped; check connection status and database availability", file=sys.stderr
            )
            raise SystemExit(1) from None
