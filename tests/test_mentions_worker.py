"""The real worker schedules mentions independently and drains reads on shutdown."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app import worker
from app.errors import AppError


def test_worker_mentions_run_during_blocked_history_and_drain_before_close(monkeypatch):
    async def exercise():
        history_started, history_release = threading.Event(), threading.Event()
        mention_started, mention_release = threading.Event(), threading.Event()
        callbacks, clients, roles, statuses = {}, [], {}, []

        class Connection:
            def scalar(self, _):
                return True

            def commit(self):
                pass

            def execute(self, _):
                pass

            def close(self):
                pass

        class Client:
            def __init__(self, *args):
                self.closed = False
                clients.append(self)

            def close(self):
                self.closed = True

        class History:
            def __init__(self, settings, sessions, client, sync):
                roles["history"] = client

            def tick(self):
                history_started.set()
                assert history_release.wait(5)

        class Mentions:
            def __init__(self, settings, sessions, client, sync):
                roles["mentions"] = client

            def run(self):
                mention_started.set()
                assert mention_release.wait(5)
                assert not roles["mentions"].closed
                return {"status": "synced", "items": 0}

        class Socket:
            def __init__(self, *args):
                self._conn = object()
                self.last_pong = time.monotonic()
                self._ping_interval = 60

            async def _ping_loop(self):
                await asyncio.Event().wait()

            async def _disconnect(self):
                self._conn = None

        engine = SimpleNamespace(connect=Connection, dispose=lambda: None)
        monkeypatch.setattr(worker, "make_database", lambda _: (engine, object()))
        monkeypatch.setattr(worker, "wait_for_database", lambda *args, **kwargs: None)
        monkeypatch.setattr(worker, "Security", lambda *args: object())
        monkeypatch.setattr(worker, "FeishuClient", Client)
        monkeypatch.setattr(worker, "SyncService", lambda *args: object())
        monkeypatch.setattr(worker, "BackgroundSync", History)
        monkeypatch.setattr(worker, "MentionSync", Mentions, raising=False)
        monkeypatch.setattr(worker, "FeishuSocket", Socket)
        monkeypatch.setattr(worker, "EventInboxHandler", lambda *args: object())
        monkeypatch.setattr(worker, "ApprovalReviewer", lambda *args, **kwargs: object())
        monkeypatch.setattr(worker, "AttentionService", lambda *args: object())
        monkeypatch.setattr(worker, "StatusNotificationService", lambda *args: object())
        monkeypatch.setattr(worker, "worker_status", lambda *args, **kwargs: statuses.append((args, kwargs)))
        monkeypatch.setattr(worker.loop, "add_signal_handler", lambda sig, callback: callbacks.setdefault(sig, callback))
        settings = SimpleNamespace(
            database_wait_seconds=1, sync_interval_seconds=60,
            approval_model_enabled=False, approval_automation_enabled=False,
            attention_enabled=False, status_notifications_enabled=False,
        )
        failures = []

        async def drive():
            try:
                assert await asyncio.to_thread(history_started.wait, 2)
                assert await asyncio.to_thread(mention_started.wait, 2), "History blocked the mention scheduler"
                assert roles["mentions"] is not roles["history"]
                assert len(clients) == len({id(client) for client in clients})
                callbacks[worker.signal.SIGTERM]()
                assert not roles["mentions"].closed
            except Exception as exc:
                failures.append(exc)
            finally:
                for callback in callbacks.values():
                    callback()
                history_release.set()
                mention_release.set()

        driver = asyncio.create_task(drive())
        await worker.run(settings)
        await driver
        if failures:
            raise failures[0]
        assert all(client.closed for client in clients)
        assert any(args[1] == "mentions" and kwargs.get("status") == "synced" for args, kwargs in statuses)

    asyncio.run(exercise())


def test_stopping_mentions_waits_for_inflight_read_without_restarting(monkeypatch):
    async def exercise():
        started, release = threading.Event(), threading.Event()
        stopping = asyncio.Event()
        calls = []

        def read():
            calls.append("started")
            started.set()
            assert release.wait(3)
            calls.append("finished")
            return {"status": "synced"}

        monkeypatch.setattr(worker, "worker_status", lambda *args, **kwargs: None)
        task = asyncio.create_task(worker.poll_mentions(
            SimpleNamespace(sync_interval_seconds=60), object(), SimpleNamespace(run=read), stopping,
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            stopping.set()
            await asyncio.to_thread(lambda: None)
            assert not task.done() and calls == ["started"]
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)
        assert calls == ["started", "finished"]

    asyncio.run(exercise())


@pytest.mark.parametrize("interval", [1, 60, 120])
def test_permissions_and_errors_keep_normal_poll_interval(monkeypatch, interval):
    statuses, waits, results = [], [], iter([
        {"status": "blocked_permission", "missing_scopes": ["search:message"]},
        RuntimeError("private detail must not reach worker status"),
        AppError("invalid private/error", "private error"),
        {"status": "synced", "items": 1},
    ])

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        async def wait(self):
            return None

    stopping = Stop()

    async def wait_once(awaitable, timeout):
        await awaitable
        waits.append(timeout)
        stopping.stopped = len(waits) == 4
        raise TimeoutError

    def read():
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(worker.asyncio, "wait_for", wait_once)
    monkeypatch.setattr(worker, "worker_status", lambda *args, **kwargs: statuses.append(kwargs))
    asyncio.run(worker.poll_mentions(
        SimpleNamespace(sync_interval_seconds=interval), object(), SimpleNamespace(run=read), stopping,
    ))
    assert all(max(60, interval) - 1 <= delay <= max(60, interval) for delay in waits)
    assert [state["status"] for state in statuses] == ["blocked_permission", "error", "error", "synced"]
    assert statuses[0]["missing_scopes"] == ["search:message"]
    assert statuses[1]["error_code"] == statuses[2]["error_code"] == "mention_sync_unavailable"


def test_short_lock_conflicts_have_only_two_fast_retries(monkeypatch):
    waits, calls = [], []

    class Stop:
        def is_set(self):
            return len(waits) >= 4

        async def wait(self):
            return None

    async def wait_once(awaitable, timeout):
        await awaitable
        waits.append(timeout)
        raise TimeoutError

    def read():
        calls.append(True)
        raise AppError("mention_sync_busy", "Cache write busy", 409)

    monkeypatch.setattr(worker.asyncio, "wait_for", wait_once)
    monkeypatch.setattr(worker, "worker_status", lambda *args, **kwargs: None)
    asyncio.run(worker.poll_mentions(
        SimpleNamespace(sync_interval_seconds=60), object(), SimpleNamespace(run=read), Stop(),
    ))
    assert len(calls) == 4
    assert waits[:2] == [5, 5] and 59 <= waits[2] <= 60 and waits[3] == 5


def test_status_database_failure_does_not_kill_mention_loop(monkeypatch):
    writes, waits = [], []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        async def wait(self):
            return None

    async def wait_once(awaitable, timeout):
        await awaitable
        waits.append(timeout)
        raise TimeoutError

    def write_status(*args, **kwargs):
        writes.append(kwargs)
        if len(writes) == 1:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(worker.asyncio, "wait_for", wait_once)
    monkeypatch.setattr(worker, "worker_status", write_status)
    asyncio.run(worker.poll_mentions(
        SimpleNamespace(sync_interval_seconds=60), object(),
        SimpleNamespace(run=lambda: {"status": "synced"}), Stop(),
    ))
    assert len(writes) == 2 and all(59 <= delay <= 60 for delay in waits)
