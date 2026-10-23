"""Reminder-only discovery reads recent allowlisted DMS cards, never chat history."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import ok
from sqlalchemy import func, select
from test_dms_attention import APP_ID, CHAT_ID, card

from app import dms_sync
from app.dms_sync import DmsCardSync, DmsWindow
from app.errors import AppError, FeishuError
from app.models import Account, Chat, Message, SyncState, utcnow


def source_card(created, identifier="om_dms", **changes):
    raw = card(created, identifier, **changes)
    raw["sender"]["tenant_key"] = "tenant_test"
    return raw


@pytest.mark.parametrize("change", ["valid", "sender", "tenant", "chat", "group", "text", "old", "not_dms"])
def test_source_filter_requires_recent_personal_card_from_trusted_app(change):
    now = utcnow().replace(microsecond=0)
    raw = source_card(now - timedelta(seconds=1))
    if change == "sender":
        raw["sender"]["id"] = "cli_other"
    elif change == "tenant":
        raw["sender"]["tenant_key"] = "other"
    elif change == "chat":
        raw["chat_id"] = "oc_other"
    elif change == "group":
        raw["chat_type"] = "group"
    elif change == "text":
        raw["msg_type"] = "text"
    elif change == "old":
        raw["create_time"] = str(int((now - timedelta(days=1)).timestamp() * 1000))
    elif change == "not_dms":
        raw["body"] = {"content": '{"title":"Unrelated app notification"}'}
    window = DmsWindow(
        ("ou_owner", "tenant_test"), None, "test",
        {"chat_id": CHAT_ID, "sender_app_id": APP_ID,
         "start_time": str(int(now.timestamp()) - 900), "end_time": str(int(now.timestamp()))},
        None, now, None,
    )
    assert DmsCardSync._trusted_card(raw, window) is (change == "valid")


@pytest.fixture
def dms(make_app, settings, monkeypatch):
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    monkeypatch.setattr(dms_sync, "utcnow", lambda: clock.now)
    settings.dms_chat_id, settings.dms_sender_app_id = CHAT_ID, APP_ID
    remote = SimpleNamespace(
        rows=[], calls=[], cursor=None, fail=False, before=None, metadata_fail=False,
    )

    def handler(request):
        assert request.headers["authorization"] == "Bearer user-access"
        remote.calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/messages"):
            assert request.url.params["container_id"] == CHAT_ID
            assert request.url.params["container_id_type"] == "chat"
            assert int(request.url.params["end_time"]) - int(request.url.params["start_time"]) == 900
            if remote.before:
                remote.before()
            if remote.fail:
                return httpx.Response(503, json={"code": 503})
            return ok({"items": deepcopy(remote.rows), "has_more": bool(remote.cursor), "page_token": remote.cursor})
        if request.url.path.endswith(f"/chats/{CHAT_ID}"):
            if remote.metadata_fail:
                return httpx.Response(403, json={"code": 99991679})
            return ok({"chat_id": CHAT_ID, "chat_mode": "p2p", "name": "DMS"})
        pytest.fail(f"Unexpected broad/history API: {request.url.path}")

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(Chat(chat_id=CHAT_ID, name="DMS", chat_mode="p2p", raw={}, generation="seed", visible=True))
    return SimpleNamespace(app=app, sessions=app.state.sessions, settings=settings, clock=clock, remote=remote)


@pytest.mark.integration
def test_new_dms_window_does_not_resume_history_or_store_unrelated_messages(dms):
    with dms.sessions.begin() as db:
        db.add(SyncState(
            resource=f"messages:{CHAT_ID}", generation="old-history", page_token="old-cursor", has_more=True,
            parameters={"start_time": "1", "end_time": "2"},
        ))
    now = dms.clock.now - timedelta(seconds=1)
    dms.remote.rows = [
        source_card(now), source_card(now, "om_text", msg_type="text"),
        source_card(now - timedelta(days=1), "om_old"),
        source_card(now, "om_other", chat_id="oc_other"),
    ]
    result = dms.app.state.sync.run("dms_cards")
    assert result["items"] == 1 and not result["has_more"]
    dms.app.state.sync.run("dms_cards")
    with dms.sessions() as db:
        assert list(db.scalars(select(Message.message_id))) == ["om_dms"]
        assert db.get(SyncState, f"messages:{CHAT_ID}").page_token == "old-cursor"
    assert all("page_token" not in query for _, query in dms.remote.calls)


@pytest.mark.integration
def test_dms_page_failure_keeps_frozen_window_and_committed_cursor(dms):
    dms.remote.rows = [source_card(dms.clock.now - timedelta(seconds=1))]
    dms.remote.cursor = "next"
    first = dms.app.state.sync.run("dms_cards")
    assert first["has_more"]
    dms.clock.now += timedelta(seconds=61)
    dms.remote.fail = True
    with pytest.raises(FeishuError):
        dms.app.state.sync.run("dms_cards")
    with dms.sessions() as db:
        assert db.get(SyncState, "dms_cards").page_token == "next"
    dms.remote.fail, dms.remote.cursor = False, None
    final = dms.app.state.sync.run("dms_cards")
    assert not final["has_more"]
    assert final["parameters"]["end_time"] == first["parameters"]["end_time"]
    assert dms.remote.calls[-1][1]["page_token"] == "next"


@pytest.mark.integration
def test_expired_dms_page_yields_to_current_window(dms):
    dms.remote.cursor = "old-page"
    dms.app.state.sync.run("dms_cards")
    dms.clock.now += timedelta(minutes=16)
    dms.remote.cursor = None
    dms.remote.rows = [source_card(dms.clock.now - timedelta(seconds=1), "om_new")]
    result = dms.app.state.sync.run("dms_cards")
    assert result["items"] == 1 and result["parameters"]["last_gap"]
    assert "page_token" not in dms.remote.calls[-1][1]


@pytest.mark.integration
@pytest.mark.parametrize("initial", ["missing", "hidden"])
def test_user_verified_dms_chat_is_created_or_recovered_without_directory_scan(dms, initial):
    with dms.sessions.begin() as db:
        chat = db.get(Chat, CHAT_ID)
        if initial == "missing":
            db.delete(chat)
        else:
            chat.visible = False
    dms.remote.rows = [source_card(dms.clock.now - timedelta(seconds=1))]
    assert dms.app.state.sync.run("dms_cards")["items"] == 1
    with dms.sessions() as db:
        assert db.get(Chat, CHAT_ID).visible
        assert db.get(Message, "om_dms")
    assert [path for path, _ in dms.remote.calls] == [
        "/open-apis/im/v1/messages", f"/open-apis/im/v1/chats/{CHAT_ID}",
    ]


@pytest.mark.integration
def test_unverified_dms_chat_does_not_fall_back_to_app_identity(dms):
    with dms.sessions.begin() as db:
        db.get(Chat, CHAT_ID).visible = False
    dms.remote.rows = [source_card(dms.clock.now - timedelta(seconds=1))]
    dms.remote.metadata_fail = True
    with pytest.raises(FeishuError):
        dms.app.state.sync.run("dms_cards")
    with dms.sessions() as db:
        assert not db.get(Chat, CHAT_ID).visible
        assert db.get(Message, "om_dms") is None
        assert db.get(SyncState, "dms_cards") is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["owner", "cursor", "chat"])
def test_changed_identity_cursor_or_chat_does_not_publish_stale_dms_read(dms, change):
    dms.remote.rows = [source_card(dms.clock.now - timedelta(seconds=1))]

    def interfere():
        with dms.sessions.begin() as db:
            if change == "owner":
                db.get(Account, 1).open_id = "ou_other"
            elif change == "chat":
                db.get(Chat, CHAT_ID).visible = False
            else:
                db.add(SyncState(resource="dms_cards", generation="concurrent", parameters={}, has_more=False))
    dms.remote.before = interfere
    with pytest.raises(AppError) as caught:
        dms.app.state.sync.run("dms_cards")
    assert caught.value.code == {
        "owner": "dms_owner_changed", "cursor": "sync_cursor_changed", "chat": "dms_chat_changed",
    }[change]
    with dms.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0
