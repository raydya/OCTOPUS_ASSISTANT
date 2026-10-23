"""Reminder-only mode retains targeted reads without reviving historical scans."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_live_sync_latency import live as live
from test_live_sync_latency import message

from app.attention import AttentionService
from app.errors import AppError
from app.models import Chat, Message, SyncState
from app.status_notifications import StatusNotificationService


@pytest.mark.parametrize("reminders_only", [True, False])
def test_reminder_only_mode_does_not_schedule_dms_history_scan(reminders_only):
    settings = SimpleNamespace(
        reminders_only=reminders_only, dms_sender_app_id="cli_dba", dms_chat_id="oc_dba",
    )

    def no_database_scan():
        pytest.fail("Reminder-only startup must not scan cached DMS history")

    service = AttentionService(settings, SimpleNamespace(begin=no_database_scan), object(), None)
    assert service._dms_history_pending is (not reminders_only)
    if reminders_only:
        service._discover_dms_history()


def test_enabling_reminder_only_mode_cancels_an_unstarted_dms_history_scan():
    settings = SimpleNamespace(
        reminders_only=False, dms_sender_app_id="cli_dba", dms_chat_id="oc_dba",
    )

    def no_database_scan():
        pytest.fail("Mode change must cancel historical discovery before any database scan")

    service = AttentionService(settings, SimpleNamespace(begin=no_database_scan), object(), None)
    assert service._dms_history_pending
    settings.reminders_only = True
    service._discover_dms_history()
    assert not service._dms_history_pending


@pytest.mark.integration
@pytest.mark.parametrize("reminders_only", [True, False])
def test_live_mention_revalidates_hidden_chat_only_in_reminder_mode(live, reminders_only):
    live.settings.reminders_only = reminders_only
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    with live.sessions.begin() as db:
        for identifier in ("oc_personal", "oc_untouched"):
            db.add(Chat(
                chat_id=identifier, name="Old directory", chat_mode="group", raw={},
                generation="old-directory", visible=False,
            ))

    assert live.reader.run()["items"] == 1

    with live.sessions() as db:
        chat = db.get(Chat, "oc_personal")
        assert chat.visible is reminders_only
        assert chat.generation == "old-directory"
        assert not db.get(Chat, "oc_untouched").visible
        source = db.get(Message, "om_live")
        assert source is not None
        assert db.get(SyncState, "chats") is None
        notifier = StatusNotificationService(live.settings, live.sessions, object())
        assert bool(notifier._mention(db, source, chat, "ou_owner")) is reminders_only
    metadata_calls = [path for path, _ in live.remote.calls if path.endswith("/chats/oc_personal")]
    assert len(metadata_calls) == int(reminders_only)
    assert not any(path.endswith("/chats") for path, _ in live.remote.calls)


@pytest.mark.integration
def test_hidden_chat_is_not_restored_from_unverified_metadata(live):
    live.settings.reminders_only = True
    live.remote.rows = {"om_live": message(live.clock.now - timedelta(seconds=1))}
    live.remote.chat_data = {"chat_id": "oc_wrong", "name": "Wrong chat", "chat_mode": "group"}
    with live.sessions.begin() as db:
        db.add(Chat(
            chat_id="oc_personal", name="Old directory", chat_mode="group", raw={},
            generation="old-directory", visible=False,
        ))

    with pytest.raises(AppError):
        live.reader.run()

    with live.sessions() as db:
        assert not db.get(Chat, "oc_personal").visible
        assert db.get(Message, "om_live") is None


@pytest.mark.integration
def test_nonpersonal_search_hit_cannot_restore_hidden_chat(live):
    live.settings.reminders_only = True
    live.remote.rows = {
        "om_live": message(live.clock.now - timedelta(seconds=1), mentions=[{"id": "all"}]),
    }
    with live.sessions.begin() as db:
        db.add(Chat(
            chat_id="oc_personal", name="Old directory", chat_mode="group", raw={},
            generation="old-directory", visible=False,
        ))

    assert live.reader.run()["items"] == 0

    with live.sessions() as db:
        assert not db.get(Chat, "oc_personal").visible
        assert db.get(Message, "om_live") is None
    assert not any(path.endswith("/chats/oc_personal") for path, _ in live.remote.calls)
