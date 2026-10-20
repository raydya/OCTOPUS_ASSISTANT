"""DMS reminders use synthetic cards and fake transport; no DMS action is executed."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_attention import FakeFeishu, add_message, change_message, raw_message, row
from test_attention import system as system

from app.attention import AttentionService
from app.errors import FeishuError
from app.models import AttentionState, Chat, Message, MessageAttention

pytestmark = pytest.mark.integration

APP_ID = "cli_dms"
CHAT_ID = "oc_dms"
TASK_KEY = "DMS-数据导出-123456"


def card(created, message_id="om_dms", *, buttons=("同意", "拒绝"), **changes):
    return {
        "message_id": message_id,
        "chat_id": CHAT_ID,
        "chat_type": "p2p",
        "msg_type": "interactive",
        "create_time": str(int(created.timestamp() * 1000)),
        "update_time": str(int(created.timestamp() * 1000)),
        "sender": {
            "id": APP_ID, "id_type": "app_id", "sender_type": "app", "tenant_key": "tenant",
        },
        "mentions": [],
        "deleted": False,
        "body": {"content": json.dumps({
            "title": TASK_KEY,
            "card_link": {"url": "https://dms.aliyun.com/?taskId=123456"},
            "elements": [[
                {"tag": "text", "text": "创建人：测试申请人\n审批人：本人\n用途：测试数据导出"},
                *({"tag": "button", "text": label} for label in buttons),
            ]],
        }, ensure_ascii=False)},
        **changes,
    }


class DmsFeishu(FakeFeishu):
    def __init__(self):
        super().__init__()
        self.source_error = None

    def message(self, message_id):
        if self.source_error:
            self.message_reads.append(message_id)
            raise self.source_error
        return super().message(message_id)

    def messages(self, chat_id, parameters, page_token):
        if chat_id == CHAT_ID:
            self.list_calls.append((chat_id, deepcopy(parameters), page_token))
            return deepcopy(self.tail)
        return super().messages(chat_id, parameters, page_token)

    def send_self_reminder(self, text, uuid):
        super().send_self_reminder(text, uuid)
        return {"message_id": f"om_reminder_{len(self.sent)}"}

    def approve_task(self, *args, **kwargs):
        pytest.fail("DMS reminder must never approve a task")


@pytest.fixture
def dms_system(system):
    system.settings.dms_sender_app_id = APP_ID
    system.settings.dms_chat_id = CHAT_ID
    system.feishu = DmsFeishu()
    with system.sessions.begin() as db:
        db.add(Chat(
            chat_id=CHAT_ID, name="DMS 审批机器人", chat_mode="p2p", raw={"chat_type": "p2p"},
            generation="test", visible=True, synced_at=system.clock.now,
        ))
    system.service = AttentionService(
        system.settings, system.sessions, system.feishu, system.model,
    )
    return system


def dms_rows(system):
    with system.sessions() as db:
        return db.scalars(
            select(MessageAttention).where(MessageAttention.source_type == "dms_card")
        ).all()


def dms_row(system):
    records = dms_rows(system)
    assert len(records) == 1
    return records[0]


def discover_card(system, raw=None):
    raw = raw or card(system.clock.now)
    add_message(system, raw["message_id"], raw=raw)
    system.service.tick()
    record = dms_row(system)
    assert record.status == "waiting"
    assert record.source_data["source_message_id"] == raw["message_id"]
    return record


def test_historical_pending_card_gets_full_fifteen_minutes_despite_read_and_chat_reply(dms_system):
    system = dms_system
    original = card(system.clock.now - timedelta(hours=3))
    record = discover_card(system, original)
    due = record.due_at
    assert due == system.clock.now + timedelta(minutes=15)
    assert record.source_data["task_key"] == TASK_KEY
    assert record.source_data["task_id"] == "123456"
    assert record.source_data["card_status"] == "pending"

    # Reading a card and replying in its chat are not a DMS approval decision.
    system.feishu.read = True
    reply = raw_message(
        system.clock.now + timedelta(seconds=1), "om_owner_reply",
        chat_id=CHAT_ID, chat_type="p2p", mentions=[],
        sender={"id": "ou_owner", "id_type": "open_id", "sender_type": "user"},
        body={"content": json.dumps({"text": "我先看看。"})},
    )
    add_message(system, "om_owner_reply", raw=reply)
    system.feishu.tail = {"items": [original, reply], "has_more": False}
    system.clock.now = due - timedelta(microseconds=1)
    system.service.tick()
    assert dms_row(system).due_at == due
    assert not system.feishu.sent and not system.feishu.calls

    system.clock.now = due
    result = system.service.tick()
    assert result["status"] == "phone_accepted"
    assert system.feishu.calls == ["om_reminder_1"]
    assert len(system.feishu.sent) == 1
    assert "123456" in system.feishu.sent[0][0] and "DMS" in system.feishu.sent[0][0]
    assert not system.model.calls
    assert system.feishu.message_reads.count("om_dms") >= 2


def test_repeat_cards_and_sync_do_not_reset_timer_or_repeat_a_submitted_task(dms_system):
    system = dms_system
    original = discover_card(system)
    due, identifier = original.due_at, original.message_id
    system.clock.now += timedelta(minutes=5)
    newer = card(system.clock.now, "om_dms_duplicate")
    add_message(system, newer["message_id"], raw=newer)
    system.service.tick()
    current = dms_row(system)
    assert current.message_id == identifier and current.due_at == due
    assert current.source_data["source_message_id"] == "om_dms_duplicate"
    change_message(system, "om_dms_duplicate", read_count=7)
    system.service.tick()
    assert dms_row(system).due_at == due

    system.clock.now = due
    assert system.service.tick()["status"] == "phone_accepted"
    assert "om_dms_duplicate" in system.feishu.message_reads
    system.clock.now += timedelta(hours=2)
    newest = card(system.clock.now, "om_dms_after_call")
    add_message(system, newest["message_id"], raw=newest)
    for _ in range(3):
        system.service.tick()
    assert len(dms_rows(system)) == 1
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.model.calls


def test_same_content_duplicate_cached_during_live_get_resumes_the_original_due_timer(
    dms_system, monkeypatch,
):
    system = dms_system
    original = discover_card(system)
    system.clock.now = original.due_at
    original_get = system.feishu.message
    injected, observed = False, []

    def get_while_sync_caches_duplicate(message_id):
        nonlocal injected
        observed.append((message_id, dms_row(system).status))
        if not injected:
            assert message_id == "om_dms"
            injected = True
            duplicate = card(system.clock.now, "om_dms_concurrent")
            # Model a sync transaction committing during the first live GET:
            # cache the new message without running attention discovery.
            add_message(system, duplicate["message_id"], raw=duplicate)
        return original_get(message_id)

    monkeypatch.setattr(system.feishu, "message", get_while_sync_caches_duplicate)
    assert system.service.tick()["status"] == "source_changed"
    changed = dms_row(system)
    assert changed.source_data["source_message_id"] == "om_dms"
    assert changed.source_hash == original.source_hash and changed.due_at == original.due_at
    assert not system.feishu.sent and not system.feishu.calls

    # Discovering the duplicate must restore waiting even though its business
    # hash is unchanged; an expired original deadline remains immediately due.
    assert system.service.tick()["status"] == "phone_accepted"
    current = dms_row(system)
    assert current.source_data["source_message_id"] == "om_dms_concurrent"
    assert current.source_hash == original.source_hash and current.due_at == original.due_at
    assert ("om_dms_concurrent", "waiting") in observed
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    system.clock.now += timedelta(hours=1)
    for _ in range(2):
        system.service.tick()
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.model.calls


@pytest.mark.parametrize("terminal", ["Approved", "已拒绝", "已撤销"])
def test_live_terminal_card_stops_call_and_stale_pending_sync_cannot_revive_it(dms_system, terminal):
    system = dms_system
    pending = card(system.clock.now)
    record = discover_card(system, pending)
    system.clock.now = record.due_at
    terminal_card = card(
        system.clock.now, buttons=(terminal,), create_time=pending["create_time"],
    )
    system.feishu.sources["om_dms"] = terminal_card
    system.service.tick()
    assert dms_row(system).status == "responded"
    assert not system.feishu.sent and not system.feishu.calls

    # An old pending snapshot delivered late must not override the live terminal state.
    change_message(system, "om_dms", **{
        key: value for key, value in pending.items() if key != "message_id"
    })
    for _ in range(2):
        system.service.tick()
    assert dms_row(system).status == "responded"
    assert not system.feishu.sent and not system.feishu.calls
    assert not system.model.calls


@pytest.mark.parametrize("change", [
    "unknown_buttons", "non_object_body", "disabled_buttons",
    "fetch_error", "sender_changed", "deleted",
])
def test_fresh_unverifiable_card_never_sends_or_calls(dms_system, change):
    system = dms_system
    record = discover_card(system)
    remote = system.feishu.sources["om_dms"]
    if change == "unknown_buttons":
        remote["body"] = card(system.clock.now, buttons=("查看详情",))["body"]
    elif change == "non_object_body":
        remote["body"] = ["unrecognized card body"]
    elif change == "disabled_buttons":
        body = json.loads(remote["body"]["content"])
        for button in body["elements"][0][1:]:
            button["disabled"] = True
        remote["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    elif change == "fetch_error":
        system.feishu.source_error = FeishuError("99991672")
    elif change == "sender_changed":
        remote["sender"] = {**remote["sender"], "id": "cli_impostor"}
    else:
        remote["deleted"] = True
    system.clock.now = record.due_at
    system.service.tick()
    current = dms_row(system)
    assert current.status != "phone_accepted"
    assert current.reminder_submitted_at is None and current.phone_submitted_at is None
    assert not system.feishu.sent and not system.feishu.calls
    assert not system.model.calls


def test_only_configured_bot_private_chat_and_tenant_can_create_dms_reminders(dms_system):
    system = dms_system
    samples = [
        card(system.clock.now, "om_other_app", sender={
            "id": "cli_impostor", "id_type": "app_id", "sender_type": "app", "tenant_key": "tenant",
        }),
        card(system.clock.now, "om_user", sender={
            "id": APP_ID, "id_type": "open_id", "sender_type": "user", "tenant_key": "tenant",
        }),
        card(system.clock.now, "om_other_tenant", sender={
            "id": APP_ID, "id_type": "app_id", "sender_type": "app", "tenant_key": "other_tenant",
        }),
        card(system.clock.now, "om_no_tenant", sender={
            "id": APP_ID, "id_type": "app_id", "sender_type": "app",
        }),
        card(system.clock.now, "om_group", chat_id="oc_team", chat_type="group"),
        card(system.clock.now, "om_raw_group", chat_type="group"),
        card(system.clock.now, "om_plain_text", msg_type="text"),
    ]
    for sample in samples:
        add_message(system, sample["message_id"], raw=sample)
    system.service.tick()
    assert not dms_rows(system)
    assert not system.model.calls and not system.feishu.sent and not system.feishu.calls

    # Matching message metadata cannot override the synchronized chat's identity.
    with system.sessions.begin() as db:
        db.get(Chat, CHAT_ID).chat_mode = "group"
    add_message(system, "om_wrong_chat_type", raw=card(system.clock.now, "om_wrong_chat_type"))
    system.service.tick()
    assert not dms_rows(system)
    with system.sessions.begin() as db:
        db.get(Chat, CHAT_ID).chat_mode = "p2p"
        db.get(Chat, CHAT_ID).visible = False
    add_message(system, "om_hidden_chat", raw=card(system.clock.now, "om_hidden_chat"))
    system.service.tick()
    assert not dms_rows(system)
    assert not system.model.calls and not system.feishu.sent and not system.feishu.calls


def test_unconfigured_dms_monitor_and_historical_terminal_cards_do_not_create_timers(dms_system):
    system = dms_system
    system.settings.dms_sender_app_id = ""
    system.service = AttentionService(
        system.settings, system.sessions, system.feishu, system.model,
    )
    add_message(system, "om_dms", raw=card(system.clock.now))
    system.service.tick()
    assert not dms_rows(system)

    system.settings.dms_sender_app_id = APP_ID
    system.service = AttentionService(
        system.settings, system.sessions, system.feishu, system.model,
    )
    terminal = card(system.clock.now - timedelta(days=1), "om_old_terminal", buttons=("已同意",))
    add_message(system, terminal["message_id"], raw=terminal)
    system.service.tick()
    assert not dms_rows(system)
    assert not system.model.calls and not system.feishu.sent and not system.feishu.calls


def test_cached_pending_card_before_both_discovery_cursors_is_found_on_enable(dms_system):
    system = dms_system
    created = system.clock.now - timedelta(days=30)
    old_card = card(created)
    system.feishu.sources["om_dms"] = deepcopy(old_card)
    with system.sessions.begin() as db:
        state = db.get(AttentionState, 1)
        assert created < state.activated_at
        db.add(Message(
            message_id="om_dms", chat_id=CHAT_ID, msg_type="interactive",
            sender_name="DMS", text=TASK_KEY, create_time=int(old_card["create_time"]),
            deleted=False, raw=old_card, synced_at=state.discovery_at - timedelta(days=1),
        ))
    system.clock.now += timedelta(days=1)
    system.service = AttentionService(
        system.settings, system.sessions, system.feishu, system.model,
    )
    system.service.tick()
    record = dms_row(system)
    assert record.status == "waiting"
    assert record.due_at == system.clock.now + timedelta(minutes=15)
    assert record.source_data["source_message_id"] == "om_dms"
    assert not system.feishu.sent and not system.feishu.calls and not system.model.calls


@pytest.mark.parametrize("change", ["unknown_buttons", "untrusted_link"])
def test_unverified_initial_card_is_visible_but_never_actionable(dms_system, change):
    system = dms_system
    pending = card(system.clock.now)
    body = json.loads(pending["body"]["content"])
    if change == "unknown_buttons":
        body["elements"][0] = [{"tag": "button", "text": "查看详情"}]
    else:
        body["card_link"]["url"] = "https://dms.aliyun.com.example.invalid/?taskId=123456"
    pending["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    add_message(system, "om_dms", raw=pending)
    system.service.tick()
    record = dms_row(system)
    assert record.status == "blocked"
    system.clock.now = record.due_at
    system.service.tick()
    assert dms_row(system).phone_submitted_at is None
    assert not system.feishu.sent and not system.feishu.calls and not system.model.calls


@pytest.mark.parametrize("disabled", [True, "false"], ids=["disabled", "malformed_disabled"])
def test_initial_disabled_pending_buttons_never_start_a_call_timer(dms_system, disabled):
    system = dms_system
    pending = card(system.clock.now)
    body = json.loads(pending["body"]["content"])
    for button in body["elements"][0][1:]:
        button["disabled"] = disabled
    pending["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    add_message(system, "om_dms", raw=pending)

    system.service.tick()
    record = dms_row(system)
    assert record.status == "blocked"
    assert record.source_data["card_status"] == "unknown"
    system.clock.now = record.due_at
    system.service.tick()
    current = dms_row(system)
    assert current.status == "blocked"
    assert current.reminder_submitted_at is None and current.phone_submitted_at is None
    assert not system.feishu.sent and not system.feishu.calls and not system.model.calls


def test_old_disabled_card_recovers_only_after_live_pending_state_and_a_new_full_timer(dms_system):
    system = dms_system
    disabled_card = card(system.clock.now - timedelta(days=10))
    body = json.loads(disabled_card["body"]["content"])
    for button in body["elements"][0][1:]:
        button["disabled"] = True
    disabled_card["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    add_message(system, "om_dms", raw=disabled_card)
    system.service.tick()
    blocked = dms_row(system)
    assert blocked.status == "blocked" and blocked.source_data["card_status"] == "unknown"

    system.clock.now = blocked.due_at
    system.service.tick()
    blocked = dms_row(system)
    assert blocked.status == "blocked"
    assert not system.feishu.sent and not system.feishu.calls

    system.clock.now = blocked.next_attempt_at
    pending_card = card(
        system.clock.now, create_time=disabled_card["create_time"],
    )
    system.feishu.sources["om_dms"] = pending_card
    with system.sessions() as db:
        assert db.get(Message, "om_dms").raw == disabled_card
    assert system.service.tick()["status"] == "source_changed"
    with system.sessions() as db:
        assert db.get(Message, "om_dms").raw == pending_card
    assert not system.feishu.sent and not system.feishu.calls

    system.service.tick()
    waiting = dms_row(system)
    assert waiting.status == "waiting" and waiting.source_data["card_status"] == "pending"
    assert waiting.due_at == system.clock.now + timedelta(minutes=15)
    system.clock.now = waiting.due_at - timedelta(microseconds=1)
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls
    system.clock.now = waiting.due_at
    assert system.service.tick()["status"] == "phone_accepted"
    system.clock.now += timedelta(hours=1)
    system.service.tick()
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.model.calls


def test_live_change_to_old_cached_card_is_discovered_again_and_gets_full_new_timer(dms_system):
    system = dms_system
    old_card = card(system.clock.now - timedelta(days=10))
    system.feishu.sources["om_dms"] = deepcopy(old_card)
    with system.sessions.begin() as db:
        state = db.get(AttentionState, 1)
        db.add(Message(
            message_id="om_dms", chat_id=CHAT_ID, msg_type="interactive",
            sender_name="DMS", text=TASK_KEY, create_time=int(old_card["create_time"]),
            deleted=False, raw=old_card, synced_at=state.discovery_at - timedelta(days=1),
        ))
    system.service.tick()
    original = dms_row(system)
    assert original.status == "waiting"

    # This edit only exists remotely. Regular recent-message synchronization
    # will not revisit the old card, so the live check must publish its update.
    system.clock.now = original.due_at
    updated_raw = deepcopy(old_card)
    body = json.loads(updated_raw["body"]["content"])
    body["elements"][0][0]["text"] += "\n补充用途：调整后的导出范围，请重新核实"
    updated_raw["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    updated_raw["update_time"] = str(int(system.clock.now.timestamp() * 1000))
    system.feishu.sources["om_dms"] = updated_raw
    with system.sessions.begin() as db:
        assert db.get(Message, "om_dms").raw == old_card
        state = db.get(AttentionState, 1)
        state.discovery_at = system.clock.now
        state.discovery_message_id = "zz_last_discovered"

    assert system.service.tick()["status"] == "source_changed"
    assert not system.feishu.sent and not system.feishu.calls
    with system.sessions() as db:
        cached = db.get(Message, "om_dms")
        state = db.get(AttentionState, 1)
        assert cached.raw == updated_raw
        assert cached.create_time == int(old_card["create_time"])
        assert cached.synced_at > state.discovery_at
    assert dms_row(system).due_at == original.due_at

    system.service.tick()
    refreshed = dms_row(system)
    assert refreshed.status == "waiting"
    assert refreshed.source_hash != original.source_hash
    assert refreshed.due_at == system.clock.now + timedelta(minutes=15)
    system.clock.now = refreshed.due_at - timedelta(microseconds=1)
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls
    system.clock.now = refreshed.due_at
    assert system.service.tick()["status"] == "phone_accepted"
    system.clock.now += timedelta(hours=1)
    for _ in range(2):
        system.service.tick()
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.model.calls


def test_changed_business_content_starts_a_new_fifteen_minute_review_window(dms_system):
    system = dms_system
    original = discover_card(system)
    system.clock.now += timedelta(minutes=10)
    body = json.loads(system.feishu.sources["om_dms"]["body"]["content"])
    body["elements"][0][0]["text"] += "\n补充用途：更新后的导出范围"
    change_message(
        system, "om_dms", body={"content": json.dumps(body, ensure_ascii=False)},
        update_time=str(int(system.clock.now.timestamp() * 1000)),
    )
    system.service.tick()
    updated = dms_row(system)
    assert updated.source_hash != original.source_hash
    assert updated.due_at == system.clock.now + timedelta(minutes=15)
    system.clock.now = original.due_at
    system.service.tick()
    assert not system.feishu.sent and not system.feishu.calls
    system.clock.now = updated.due_at
    assert system.service.tick()["status"] == "phone_accepted"
    assert not system.model.calls


def test_ack_during_reminder_send_blocks_phone_and_later_cards_cannot_reactivate(dms_system):
    system = dms_system
    record = discover_card(system)
    system.feishu.send_callback = lambda: system.service.acknowledge(record.message_id)
    system.clock.now = record.due_at
    assert system.service.tick()["status"] == "acknowledged"
    assert len(system.feishu.sent) == 1 and not system.feishu.calls
    assert dms_row(system).phone_submitted_at is None

    system.clock.now += timedelta(hours=1)
    duplicate = card(system.clock.now, "om_dms_after_ack")
    add_message(system, duplicate["message_id"], raw=duplicate)
    system.service.tick()
    assert dms_row(system).status == "acknowledged"
    assert len(system.feishu.sent) == 1 and not system.feishu.calls
    assert not system.model.calls


def test_overdue_unsubmitted_dms_timer_survives_restart_and_checks_current_card(dms_system):
    system = dms_system
    record = discover_card(system)
    system.clock.now = record.due_at + timedelta(minutes=10)
    system.service = AttentionService(
        system.settings, system.sessions, system.feishu, system.model,
    )
    assert system.service.tick()["status"] == "phone_accepted"
    assert dms_row(system).due_at == record.due_at
    assert system.feishu.message_reads.count("om_dms") >= 2
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1
    assert not system.model.calls


@pytest.mark.parametrize("first_source", ["message", "dms_card"])
def test_dms_and_important_mentions_share_one_call_cooldown(dms_system, first_source):
    system = dms_system
    if first_source == "message":
        add_message(system)
        system.service.tick()
        first = row(system)
        second = discover_card(system)
    else:
        first = discover_card(system)
        add_message(system)
        system.service.tick()
        second = row(system)
    system.clock.now = first.due_at
    assert system.service.tick()["status"] == "phone_accepted"
    system.clock.now = second.due_at
    system.service.tick()
    waiting = row(system, second.message_id)
    cooldown_end = first.due_at + timedelta(minutes=30)
    assert waiting.status == "waiting" and waiting.next_attempt_at == cooldown_end
    assert len(system.feishu.sent) == len(system.feishu.calls) == 1

    system.clock.now = cooldown_end
    assert system.service.tick()["status"] == "phone_accepted"
    assert len(system.feishu.sent) == len(system.feishu.calls) == 2
    assert len(system.model.calls) == 1
