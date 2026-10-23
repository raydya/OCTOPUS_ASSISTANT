"""The dedicated user-mention reader bypasses historical sync, without sending."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import ok
from sqlalchemy import func, select

from app import events, mention_sync, status_notifications
from app.events import BackgroundSync
from app.mention_sync import MentionSync
from app.models import (
    Account,
    ApprovalTask,
    Chat,
    Message,
    StatusNoticeSource,
    SyncJob,
    SyncState,
    WorkerState,
    utcnow,
)
from app.status_notifications import StatusNotificationService
from app.worker import sync_mentions_once

pytestmark = pytest.mark.integration


def message(now, identifier="om_live", **changes):
    return {
        "message_id": identifier, "chat_id": "oc_personal", "msg_type": "text",
        "create_time": str(int(now.timestamp() * 1000)),
        "sender": {"id": "ou_other", "id_type": "open_id", "sender_type": "user"},
        "body": {"content": '{"text":"@owner please review"}'},
        "mentions": [{"id": "ou_owner", "id_type": "open_id", "key": "@owner"}],
        **changes,
    }


def page(*identifiers, cursor=None, notice=""):
    return {
        "items": [{"meta_data": {"message_id": value}} for value in identifiers],
        "has_more": cursor is not None, "page_token": cursor, "notice": notice,
    }


class Remote:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.search = lambda _: page(*self.rows)
        self.fail_mget = False
        self.before_mget = None
        self.chat_data = {"name": "Personal group", "chat_mode": "group"}

    def __call__(self, request):
        assert request.headers["authorization"] == "Bearer user-access"
        path = request.url.path
        self.calls.append((path, request.url.params.get("page_token")))
        if path.endswith("/messages/search"):
            assert request.url.params["page_size"] == "30"
            return ok(self.search(request.url.params.get("page_token")))
        if path.endswith("/messages/mget"):
            if self.before_mget:
                self.before_mget()
            if self.fail_mget:
                return httpx.Response(503, json={"code": 503})
            return ok({"items": [self.rows[key] for key in request.url.params.get_list("message_ids")
                                 if key in self.rows]})
        if path.endswith("/chats/oc_personal"):
            return ok(self.chat_data)
        if path.endswith("/chats"):
            return ok({"items": [], "has_more": False})
        if path.endswith("/tasks"):
            return ok({"tasks": [], "has_more": False})
        if path.endswith("/instances/detail"):
            return ok({"instance_code": "i_old", "form": "[]"})
        pytest.fail(f"Unexpected API path: {path}")


@pytest.fixture
def live(make_app, settings, monkeypatch):
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    monkeypatch.setattr(events, "utcnow", lambda: clock.now)
    monkeypatch.setattr(mention_sync, "utcnow", lambda: clock.now)
    remote = Remote()
    app = make_app(remote)
    with app.state.sessions.begin() as db:
        account = db.get(Account, 1)
        account.scope = " ".join(set(account.scope.split()) | {"search:message"})
    worker = BackgroundSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    worker.next_snapshot = worker.next_subscription = clock.now + timedelta(days=1)
    reader = MentionSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    return SimpleNamespace(
        app=app, sessions=app.state.sessions, settings=settings, remote=remote,
        worker=worker, reader=reader, clock=clock,
        poll=lambda: sync_mentions_once(app.state.sessions, reader),
    )


@pytest.mark.parametrize("history_resource", ["instance:i_old", "approvals:2", "approvals:18"])
def test_recent_personal_mention_and_history_have_separate_ticks(live, history_resource):
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    with live.sessions.begin() as db:
        db.add(ApprovalTask(
            task_id="task_old", topic="2", instance_code="i_old", title="Old approval",
            status="2", initiator_name="", raw={}, generation="test", visible=True,
        ))
        db.add(SyncJob(resource=history_resource, next_attempt_at=live.clock.now - timedelta(days=1)))
    live.poll()
    live.worker.tick()
    with live.sessions() as db:
        assert db.get(Message, "om_live") is not None
        assert db.get(Chat, "oc_personal").visible
        assert db.get(WorkerState, "mentions").status == "synced"
        assert db.get(SyncJob, history_resource) is None  # History still makes progress.
    assert live.remote.calls[0][0].endswith("/messages/search")
    assert live.remote.calls[-1][0].endswith(("/tasks", "/instances/detail"))


def test_missing_scope_does_not_call_search_or_interrupt_history(live):
    with live.sessions.begin() as db:
        account = db.get(Account, 1)
        account.scope = " ".join(value for value in account.scope.split() if value != "search:message")
        db.add(SyncJob(resource="approvals:2", next_attempt_at=live.clock.now - timedelta(seconds=1)))
    live.poll()
    live.worker.tick()
    with live.sessions() as db:
        state = db.get(WorkerState, "mentions")
        assert state.status == "blocked_permission"
        assert state.details["missing_scopes"] == ["search:message"]
        assert db.get(SyncState, "mentions") is None
        assert db.get(SyncJob, "approvals:2") is None
    assert len(live.remote.calls) == 1 and live.remote.calls[0][0].endswith("/tasks")


def test_paginated_failure_resumes_committed_cursor_and_overlap_is_idempotent(live):
    live.remote.rows = {
        key: message(live.clock.now - timedelta(seconds=1), key) for key in ("om_first", "om_second")
    }
    live.remote.search = lambda cursor: page("om_first", cursor="next") if cursor is None else page("om_second")
    first = live.reader.run()
    assert first["has_more"] and first["pages"] == 1
    live.remote.fail_mget = True
    live.poll()
    with live.sessions() as db:
        state = db.get(SyncState, "mentions")
        assert state.page_token == "next" and state.completed_at is None
        assert db.get(Message, "om_first") and not db.get(Message, "om_second")
        assert db.get(WorkerState, "mentions").status == "error"
    calls = len(live.remote.calls)
    live.worker.tick()
    assert len(live.remote.calls) == calls  # History never schedules mention reads.
    live.remote.fail_mget = False
    live.clock.now += timedelta(seconds=61)
    restarted = MentionSync(live.settings, live.sessions, live.app.state.feishu, live.app.state.sync)
    assert not restarted.run()["has_more"]
    live.remote.search = lambda _: page("om_first", "om_second")
    restarted.run()
    with live.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 2
        assert db.get(SyncState, "mentions").completed_at is not None


def test_search_warning_caches_verified_hits_without_completing_checkpoint(live):
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    live.remote.search = lambda _: page("om_live", notice="partial visibility")
    result = live.reader.run()
    assert result["status"] == "partial" and result["notice_present"]
    with live.sessions() as db:
        state = db.get(SyncState, "mentions")
        assert state.has_more and state.page_token is None and state.completed_at is None
        assert db.get(Message, "om_live")


def test_repeated_cursor_does_not_loop_or_advance_checkpoint(live):
    live.settings.sync_page_limit = 3
    live.remote.search = lambda cursor: page(cursor={None: "p1", "p1": "p2", "p2": "p1"}[cursor])
    live.poll()
    assert len(live.remote.calls) == 3
    with live.sessions() as db:
        assert db.get(SyncState, "mentions").page_token == "p2"
        assert db.get(WorkerState, "mentions").error_code == "mention_invalid_pagination"


def test_only_explicit_owner_mentions_in_window_are_saved(live):
    created = live.clock.now - timedelta(seconds=1)
    rows = [
        message(created),
        message(created, "om_all", mentions=[{"id": "all", "id_type": "open_id"}]),
        message(created, "om_self", sender={"id": "ou_owner", "id_type": "open_id", "sender_type": "user"}),
        message(created, "om_bot", sender={"id": "cli_test", "id_type": "app_id", "sender_type": "app"}),
        message(created - timedelta(days=1), "om_old"),
        message(created, "om_deleted", deleted=True),
    ]
    live.remote.rows = {row["message_id"]: row for row in rows}
    live.reader.run()
    with live.sessions() as db:
        assert list(db.scalars(select(Message.message_id))) == ["om_live"]


@pytest.mark.parametrize("change", ["owner", "cursor", "chat_metadata"])
def test_unverified_page_never_advances_or_writes_messages(live, change):
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    live.remote.search = lambda _: page("om_live")
    if change == "chat_metadata":
        live.remote.chat_data = {"chat_id": "oc_other", "name": "Wrong", "chat_mode": "group"}
    else:
        def alter():
            with live.sessions.begin() as db:
                if change == "owner":
                    db.get(Account, 1).open_id = "ou_changed"
                else:
                    db.get(SyncState, "mentions").generation = "concurrent-generation"
        live.remote.before_mget = alter
    live.poll()
    with live.sessions() as db:
        state = db.get(SyncState, "mentions")
        assert state.page_token is None and state.completed_at is None and state.has_more
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        assert db.get(WorkerState, "mentions").status == "error"


def test_missing_hit_does_not_block_valid_hits_or_later_pages(live):
    live.remote.rows = {
        key: message(live.clock.now - timedelta(seconds=1), key) for key in ("om_first", "om_second")
    }
    live.remote.search = lambda cursor: (
        page("om_missing", "om_first", cursor="second") if cursor is None else page("om_second")
    )
    first = live.reader.run()
    assert first["status"] == "partial" and first["missing_count"] == 1
    with live.sessions() as db:
        assert db.get(Message, "om_first")
        assert db.get(SyncState, "mentions").page_token == "second"
    second = live.reader.run()
    assert second["status"] == "partial" and not second["has_more"]
    with live.sessions() as db:
        state = db.get(SyncState, "mentions")
        assert state.completed_at is None and not state.has_more
        assert state.parameters["last_gap"]["reason"] == "missing_message_details"
        assert db.get(Message, "om_second")
    # Missing/deleted IDs may vanish from the search index on the next overlap.
    live.remote.search = lambda _: page("om_first", "om_second")
    assert live.reader.run()["status"] == "synced"


@pytest.mark.parametrize("change", ["deleted", "edited", "unknown_update_time"])
def test_inflight_mention_read_cannot_roll_back_a_newer_cached_message(live, change):
    old = message(live.clock.now - timedelta(seconds=1))
    if change == "edited":
        old["update_time"] = old["create_time"]
    live.remote.rows = {"om_live": old}
    current = deepcopy(old)
    current["body"] = {"content": '{"text":"latest edit"}'}
    if change == "deleted":
        current["deleted"] = True
        current["body"] = {"content": ""}
    if change == "edited":
        current["update_time"] = str(int(live.clock.now.timestamp() * 1000))

    def concurrent_sync():
        with live.sessions.begin() as db:
            db.add(Message(
                message_id="om_live", chat_id="oc_personal", msg_type="text",
                sender_name="Other", text="" if change == "deleted" else "latest edit",
                create_time=int(old["create_time"]), deleted=change == "deleted", raw=current,
                synced_at=live.clock.now + timedelta(milliseconds=1),
            ))
    live.remote.before_mget = concurrent_sync
    live.reader.run()
    with live.sessions() as db:
        cached = db.get(Message, "om_live")
        assert cached.raw == current
        assert cached.deleted is (change == "deleted")


def test_new_chat_survives_finishing_an_older_directory_snapshot(live):
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    with live.sessions.begin() as db:
        db.add(SyncState(
            resource="chats", generation="directory-in-progress", parameters={},
            page_token="old-final-page", has_more=True,
        ))
    live.reader.run()
    with live.sessions() as db:
        assert db.get(Chat, "oc_personal").generation == "directory-in-progress"
        assert db.get(SyncState, "chats").page_token == "old-final-page"
    live.app.state.sync.run("chats")
    with live.sessions() as db:
        assert db.get(Chat, "oc_personal").visible
        assert not db.get(SyncState, "chats").has_more


def test_existing_chat_membership_and_directory_generation_are_preserved(live):
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    with live.sessions.begin() as db:
        db.add(Chat(
            chat_id="oc_personal", name="Cached name", chat_mode="group", raw={},
            generation="directory-generation", visible=False,
        ))
        db.add(SyncState(resource="chats", generation="full-snapshot", parameters={}, has_more=True, page_token="cursor"))
    live.reader.run()
    with live.sessions() as db:
        chat, directory = db.get(Chat, "oc_personal"), db.get(SyncState, "chats")
        assert chat.generation == "directory-generation" and chat.visible is False
        assert chat.name == "Cached name" and directory.generation == "full-snapshot"
        assert directory.page_token == "cursor"


def test_expired_partial_window_is_abandoned_for_current_mentions(live):
    live.remote.search = lambda _: page(cursor="old_cursor")
    live.reader.run()
    live.clock.now += timedelta(days=1)
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    live.remote.search = lambda cursor: page("om_live") if cursor is None else pytest.fail("Old cursor blocked current mentions")
    result = live.reader.run()
    assert result["last_gap"] is not None
    assert result["window_start"] == (live.clock.now - timedelta(minutes=15)).isoformat()
    with live.sessions() as db:
        assert db.get(Message, "om_live")


def test_delayed_page_keeps_source_age_gate_for_status_notices(live, monkeypatch):
    from test_status_notifications import FakeStatusFeishu

    monkeypatch.setattr(status_notifications, "utcnow", lambda: live.clock.now)
    live.settings.status_notifications_enabled = True
    sender = FakeStatusFeishu()
    notices = StatusNotificationService(live.settings, live.sessions, sender)
    notices.tick()
    live.clock.now += timedelta(seconds=1)
    created = live.clock.now
    live.clock.now += timedelta(minutes=14)
    live.remote.search = lambda _: page(cursor="delayed_page")
    live.reader.run()
    live.clock.now += timedelta(minutes=2)
    # A recently completed search window may still include older messages on
    # later pages. Cache them with source timestamps, never as new receipts.
    live.remote.rows = {"om_live": message(created)}
    live.remote.search = lambda _: page("om_live")
    live.reader.run()
    notices.tick()
    with live.sessions() as db:
        assert db.get(Message, "om_live").create_time == int(created.timestamp() * 1000)
        assert db.scalar(select(StatusNoticeSource)).notify_changes is False
    assert not sender.sent
