import importlib.util
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import login, ok
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.errors import AppError
from app.models import Chat, Message, SyncState, utcnow

pytestmark = pytest.mark.integration
BASE_TIME = 1_710_000_000_000


def chat_row(identifier="oc_one", *, activity=None, **changes):
    return Chat(**{
        "chat_id": identifier, "name": identifier, "chat_mode": "group",
        "raw": {}, "generation": "seed", "visible": True,
        "last_activity_time": activity,
        **changes,
    })


def source_message(identifier, created, **changes):
    return {
        "message_id": identifier, "msg_type": "text", "create_time": str(created),
        "sender": {"id": "ou_peer", "sender_name": "同事"},
        "body": {"content": '{"text":"测试消息"}'},
        **changes,
    }


def test_chat_api_uses_source_activity_descending_with_stable_filter_and_pagination(make_app):
    app = make_app(lambda _: pytest.fail("Listing cached chats must not call Feishu"))
    now = utcnow()
    with app.state.sessions.begin() as db:
        db.add_all([
            chat_row("oc_new", activity=300, name="运维", synced_at=now - timedelta(days=30)),
            chat_row("oc_b", activity=200, name="开发 B", synced_at=now),
            chat_row("oc_a", activity=200, name="开发 A", synced_at=now - timedelta(days=1)),
            chat_row("oc_old", activity=100, name="财务", synced_at=now),
            chat_row("oc_none", activity=None, name="开发未同步", synced_at=now + timedelta(days=1)),
            chat_row("oc_hidden", activity=999, name="开发隐藏", visible=False),
        ])
    with TestClient(app, base_url="http://localhost:8000") as client:
        login(client)
        all_rows = client.get("/api/chats").json()["items"]
        assert [row["chat_id"] for row in all_rows] == [
            "oc_new", "oc_a", "oc_b", "oc_old", "oc_none",
        ]
        assert [row["last_activity_time"] for row in all_rows] == [300, 200, 200, 100, None]
        pages = [
            client.get("/api/chats", params={"limit": 2, "offset": offset}).json()
            for offset in (0, 2, 4)
        ]
        assert [page["has_more"] for page in pages] == [True, True, False]
        assert [row["chat_id"] for page in pages for row in page["items"]] == [
            row["chat_id"] for row in all_rows
        ]
        filtered = client.get("/api/chats", params={
            "q": "开发", "limit": 1, "offset": 1,
        }).json()
        assert [row["chat_id"] for row in filtered["items"]] == ["oc_b"]
        assert filtered["has_more"] is True
        last = client.get("/api/chats", params={"q": "开发", "limit": 1, "offset": 2}).json()
        assert [row["chat_id"] for row in last["items"]] == ["oc_none"]
        assert last["has_more"] is False


def test_new_edited_and_recalled_messages_advance_activity_but_resync_never_moves_it_back(make_app):
    current = {"items": [
        source_message("om_new", BASE_TIME + 100),
        source_message("om_old", BASE_TIME),
    ]}

    def handler(request):
        if request.url.path == "/open-apis/im/v1/chats":
            return ok({"items": [{
                "chat_id": "oc_one", "name": "更新后的会话名称", "chat_mode": "group",
            }], "has_more": False})
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.url.params["container_id"] == "oc_one"
        return ok({"items": deepcopy(current["items"]), "has_more": False})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(chat_row())

    def synchronize_and_expect(expected):
        app.state.sync.run("messages:oc_one", restart=True)
        with app.state.sessions() as db:
            assert db.get(Chat, "oc_one").last_activity_time == expected

    synchronize_and_expect(BASE_TIME + 100)
    current["items"] = [source_message(
        "om_old", BASE_TIME, update_time=str(BASE_TIME + 200),
        body={"content": '{"text":"编辑后的内容"}'},
    )]
    synchronize_and_expect(BASE_TIME + 200)
    current["items"] = [source_message(
        "om_new", BASE_TIME + 100, update_time=str(BASE_TIME + 300),
        deleted=True, body={"content": '{"text":"recalled-private-body"}'},
    )]
    synchronize_and_expect(BASE_TIME + 300)
    with app.state.sessions() as db:
        recalled = db.get(Message, "om_new")
        assert recalled.text == "消息已撤回"
        assert "recalled-private-body" not in json.dumps(recalled.raw)
    current["items"] = [
        source_message("om_history", BASE_TIME - 1_000, update_time="not-a-timestamp"),
        source_message("om_old", BASE_TIME),
    ]
    synchronize_and_expect(BASE_TIME + 300)
    app.state.sync.run("chats", restart=True)
    with app.state.sessions() as db:
        chat = db.get(Chat, "oc_one")
        assert chat.name == "更新后的会话名称"
        assert chat.last_activity_time == BASE_TIME + 300


def test_thread_reply_updates_parent_chat_activity_and_not_an_unrelated_chat(make_app):
    def handler(request):
        assert request.url.params["container_id_type"] == "thread"
        assert request.url.params["container_id"] == "omt_activity"
        assert "start_time" not in request.url.params
        return ok({"items": [source_message(
            "om_reply", BASE_TIME + 50, parent_id="om_root", update_time=str(BASE_TIME + 500),
        )], "has_more": False})

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add_all([
            chat_row(activity=BASE_TIME),
            chat_row("oc_other", activity=BASE_TIME + 1_000),
        ])
        db.add(Message(
            message_id="om_root", chat_id="oc_one", thread_id="omt_activity",
            msg_type="text", sender_name="同事", text="话题根消息",
            create_time=BASE_TIME, raw=source_message("om_root", BASE_TIME),
        ))
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/sync/threads/omt_activity", json={}, headers=login(client),
        )
        assert response.status_code == 200
    with app.state.sessions() as db:
        assert db.get(Chat, "oc_one").last_activity_time == BASE_TIME + 500
        assert db.get(Chat, "oc_other").last_activity_time == BASE_TIME + 1_000
        assert db.get(Chat, "omt_activity") is None
        reply = db.get(Message, "om_reply")
        assert reply.chat_id == "oc_one" and reply.thread_id == "omt_activity"


def test_failed_message_page_rolls_back_activity_and_retries_from_the_same_cursor(make_app):
    state = {"fail": True}
    cursors = []

    def handler(request):
        cursor = request.url.params.get("page_token")
        cursors.append(cursor)
        if cursor is None:
            return ok({
                "items": [source_message("om_first", BASE_TIME + 100)],
                "has_more": True, "page_token": "next",
            })
        return ok({
            "items": [source_message("om_later", BASE_TIME + 900)],
            "has_more": state["fail"], "page_token": "next",
        })

    app = make_app(handler)
    with app.state.sessions.begin() as db:
        db.add(chat_row(activity=BASE_TIME))
    assert app.state.sync.run("messages:oc_one")["has_more"] is True
    with pytest.raises(AppError) as error:
        app.state.sync.run("messages:oc_one")
    assert error.value.code == "invalid_pagination"
    with app.state.sessions() as db:
        assert db.get(Chat, "oc_one").last_activity_time == BASE_TIME + 100
        assert db.get(Message, "om_later") is None
        assert db.get(SyncState, "messages:oc_one").page_token == "next"
    state["fail"] = False
    assert app.state.sync.run("messages:oc_one")["has_more"] is False
    with app.state.sessions() as db:
        assert db.get(Chat, "oc_one").last_activity_time == BASE_TIME + 900
        assert db.get(Message, "om_later") is not None
        assert db.get(SyncState, "messages:oc_one").page_token is None
    assert cursors == [None, "next", "next"]


def test_activity_migration_backfills_legacy_messages_in_an_isolated_schema(database):
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0007_chat_activity.py"
    spec = importlib.util.spec_from_file_location("chat_activity_migration_under_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "chat_activity_" + uuid4().hex
    with database.connect() as connection:
        original_revision = tuple(connection.scalars(text("SELECT version_num FROM alembic_version")))
        connection.rollback()
        transaction = connection.begin()
        try:
            # Transactional DDL and SET LOCAL keep the application's tables and
            # alembic_version untouched, including when an assertion fails.
            connection.execute(text(f"CREATE SCHEMA {schema}"))
            connection.execute(text(f"SET LOCAL search_path TO {schema}"))
            connection.execute(text("CREATE TABLE chats (chat_id text PRIMARY KEY)"))
            connection.execute(text(
                "CREATE TABLE messages (chat_id text NOT NULL, create_time bigint, raw jsonb)"
            ))
            expected = {
                "edited": BASE_TIME + 500, "newest": BASE_TIME + 900,
                "empty": None, "invalid": None, "fallback": BASE_TIME,
                "upper_bound": 253402300799999,
            }
            connection.execute(
                text("INSERT INTO chats (chat_id) VALUES (:chat_id)"),
                [{"chat_id": identifier} for identifier in expected],
            )
            values = [
                ("edited", BASE_TIME, {"update_time": str(BASE_TIME + 500)}),
                ("edited", BASE_TIME - 100, {}),
                ("newest", BASE_TIME + 900, {"update_time": str(BASE_TIME + 200)}),
                ("fallback", BASE_TIME, {"update_time": "999999999999999999999999999"}),
                ("upper_bound", 0, {"update_time": "253402300799999"}),
                ("upper_bound", 0, {"update_time": "253402300800000"}),
                *[("invalid", 0, {"update_time": invalid}) for invalid in (
                    "not-a-time", "999999999999999999999999999", True, -1, None, 1.5,
                )],
            ]
            connection.execute(
                text(
                    "INSERT INTO messages (chat_id, create_time, raw) "
                    "VALUES (:chat_id, :created, CAST(:raw AS jsonb))"
                ),
                [
                    {"chat_id": identifier, "created": created, "raw": json.dumps(raw)}
                    for identifier, created, raw in values
                ],
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            actual = dict(connection.execute(text(
                "SELECT chat_id, last_activity_time FROM chats"
            )).all())
            assert actual == expected
            assert "ix_chats_activity_order" in set(connection.scalars(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
                {"schema": schema},
            ))
        finally:
            transaction.rollback()
        assert tuple(connection.scalars(text(
            "SELECT version_num FROM alembic_version"
        ))) == original_revision
