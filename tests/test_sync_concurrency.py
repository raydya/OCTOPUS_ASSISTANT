"""Historical HTTP reads must not reserve the cache writer used by live mentions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event, Lock

import pytest
from conftest import ok
from sqlalchemy import text

from app.errors import AppError, FeishuError
from app.mention_sync import MentionSync
from app.models import Account, ApprovalInstance, ApprovalTask, Chat, Message, SyncState, utcnow

pytestmark = pytest.mark.integration


def mentioned_message():
    return {
        "message_id": "om_live", "chat_id": "oc_live", "msg_type": "text",
        "create_time": str(int((utcnow() - timedelta(seconds=1)).timestamp() * 1000)),
        "sender": {"id": "ou_other", "id_type": "open_id", "sender_type": "user"},
        "body": {"content": '{"text":"@owner review"}'},
        "mentions": [{"id": "ou_owner", "id_type": "open_id", "key": "@owner"}],
    }


@pytest.mark.parametrize("check", ["write_lock", "live_message"])
def test_blocked_history_http_does_not_block_live_cache_writes(make_app, settings, check):
    entered, release = Event(), Event()
    raw = mentioned_message()

    def handler(request):
        if request.url.path.endswith("/chats"):
            entered.set()
            assert release.wait(5), "test did not release historical HTTP"
            return ok({"items": [], "has_more": False})
        if request.url.path.endswith("/messages/search"):
            return ok({"items": [{"meta_data": {"message_id": raw["message_id"]}}], "has_more": False})
        if request.url.path.endswith("/messages/mget"):
            return ok({"items": [raw]})
        if request.url.path.endswith("/chats/oc_live"):
            return ok({"chat_mode": "group", "name": "live"})
        pytest.fail(f"Unexpected API: {request.url.path}")

    app = make_app(handler)
    mentions = MentionSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        history = pool.submit(app.state.sync.run, "chats")
        try:
            assert entered.wait(3)
            assert app.state.sessions.kw["bind"].pool.checkedout() == 0, "HTTP retained a database connection"
            if check == "write_lock":
                with app.state.sessions.begin() as db:
                    assert db.scalar(text("SELECT pg_try_advisory_xact_lock(81024002)"))
            else:
                assert mentions.run()["items"] == 1
                with app.state.sessions() as db:
                    assert db.get(Message, "om_live") is not None
                    assert db.get(Chat, "oc_live").visible
                assert not history.done(), "live insert should finish before releasing history"
        finally:
            release.set()
            history.result(timeout=3)
    if check == "live_message":
        with app.state.sessions() as db:
            assert db.get(Chat, "oc_live").visible, "an older empty snapshot hid a newly verified chat"


@pytest.mark.parametrize("resuming", [False, True])
def test_late_batch_cannot_overwrite_another_readers_committed_cursor(make_app, resuming):
    entered, release, counter_lock = Event(), Event(), Lock()
    calls = 0

    def handler(_request):
        nonlocal calls
        with counter_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(5)
        identifier = "oc_stale" if first else "oc_new"
        return ok({
            "items": [{"chat_id": identifier, "name": identifier}],
            "has_more": not first, "page_token": "next-new" if not first else None,
        })

    app = make_app(handler)
    if resuming:
        with app.state.sessions.begin() as db:
            db.add(SyncState(
                resource="chats", generation="original", parameters={},
                has_more=True, page_token="previous",
            ))
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(app.state.sync.run, "chats")
        try:
            assert entered.wait(3)
            assert app.state.sync.run("chats")["has_more"]
        finally:
            release.set()
        with pytest.raises(AppError) as caught:
            stale.result(timeout=3)
        assert caught.value.code == "sync_cursor_changed"
    with app.state.sessions() as db:
        state = db.get(SyncState, "chats")
        assert state.has_more and state.page_token == "next-new"
        assert db.get(Chat, "oc_new") and not db.get(Chat, "oc_stale")


def test_late_instance_read_cannot_overwrite_a_newer_instance(make_app):
    entered, release, counter_lock = Event(), Event(), Lock()
    calls = 0

    def handler(_request):
        nonlocal calls
        with counter_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(5)
        return ok({"instance_code": "i_one", "status": "PENDING" if first else "APPROVED"})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(ApprovalTask(
            task_id="t_one", instance_code="i_one", topic="1", title="approval",
            status="1", initiator_name="", raw={}, generation="seed",
        ))
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(app.state.sync.run, "instance:i_one")
        try:
            assert entered.wait(3)
            app.state.sync.run("instance:i_one")
        finally:
            release.set()
        with pytest.raises(AppError) as caught:
            stale.result(timeout=3)
        assert caught.value.code == "sync_cursor_changed"
    with app.state.sessions() as db:
        assert db.get(ApprovalInstance, "i_one").raw["status"] == "APPROVED"


@pytest.mark.parametrize("slow_reader", ["history", "mention"])
@pytest.mark.parametrize("change", ["edited", "deleted", "missing_version", "equal_version"])
def test_old_message_read_cannot_roll_back_newer_live_or_historical_cache(
    make_app, settings, slow_reader, change,
):
    entered, release = Event(), Event()
    old = mentioned_message()
    old["update_time"] = old["create_time"]
    current = deepcopy(old)
    current["body"] = {"content": '{"text":"latest edit"}'}
    if change == "edited":
        current["update_time"] = str(int(old["update_time"]) + 1)
    elif change == "deleted":
        current["deleted"] = True
        current["body"] = {"content": ""}
    elif change == "missing_version":
        old.pop("update_time")
        current.pop("update_time")

    def handler(request):
        path = request.url.path
        if path.endswith("/messages/search"):
            return ok({"items": [{"meta_data": {"message_id": old["message_id"]}}], "has_more": False})
        channel = "mention" if path.endswith("/messages/mget") else "history"
        if channel == slow_reader:
            entered.set()
            assert release.wait(5)
            row = old
        else:
            row = current
        return ok({"items": [row], "has_more": False})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(Chat(chat_id="oc_live", name="live", chat_mode="group", raw={}, generation="seed"))
    mentions = MentionSync(settings, app.state.sessions, app.state.feishu, app.state.sync)
    readers = {"history": lambda: app.state.sync.run("messages:oc_live"), "mention": mentions.run}
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(readers[slow_reader])
        try:
            assert entered.wait(3)
            if slow_reader == "history" and change == "deleted":
                # Live discovery intentionally excludes recalled messages. Its
                # message recheck path can still cache a confirmed recall.
                with app.state.sessions.begin() as db:
                    app.state.sync._store(db, "messages", current, "oc_live", "oc_live", "live")
            else:
                readers["mention" if slow_reader == "history" else "history"]()
        finally:
            release.set()
        stale.result(timeout=3)
    with app.state.sessions() as db:
        row = db.get(Message, "om_live")
        assert row.raw == current
        assert row.deleted is (change == "deleted")


def test_account_change_during_http_does_not_publish_old_accounts_data(make_app):
    entered, release = Event(), Event()

    def handler(_request):
        entered.set()
        assert release.wait(5)
        return ok({"items": [{"chat_id": "oc_wrong", "name": "old owner"}], "has_more": False})

    app = make_app(handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale = pool.submit(app.state.sync.run, "chats")
        try:
            assert entered.wait(3)
            with app.state.sessions.begin() as db:
                db.get(Account, 1).open_id = "ou_changed"
        finally:
            release.set()
        with pytest.raises(AppError) as caught:
            stale.result(timeout=3)
        assert caught.value.code == "sync_owner_changed"
    with app.state.sessions() as db:
        assert db.get(Chat, "oc_wrong") is None
        assert db.get(SyncState, "chats") is None


def test_invalid_later_page_rolls_back_all_rows_and_cursor(make_app, settings):
    settings.sync_page_limit = 2

    def handler(request):
        if request.url.params.get("page_token") is None:
            return ok({"items": [{"chat_id": "oc_first"}], "has_more": True, "page_token": "next"})
        return ok({"items": [{}], "has_more": False})

    app = make_app(handler)
    with pytest.raises(FeishuError):
        app.state.sync.run("chats")
    with app.state.sessions() as db:
        assert db.get(Chat, "oc_first") is None
        assert db.get(SyncState, "chats") is None
