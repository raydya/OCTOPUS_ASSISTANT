import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.attention as attention
from app.attention import AttentionService, _verified_result, is_personal_mention, source_hash
from app.errors import AppError, FeishuError
from app.models import Account, AttentionState, Chat, Message, MessageAttention, utcnow

IMPORTANT = "线上结算服务持续不可用，请立即确认并处理故障。"


def raw_message(created, message_id="om_source", **changes):
    return {
        "message_id": message_id, "chat_id": "oc_team", "msg_type": "text",
        "create_time": str(int(created.timestamp() * 1000)),
        "sender": {"id": "ou_sender", "id_type": "open_id", "sender_type": "user"},
        "mentions": [{"id": "ou_owner", "id_type": "open_id", "key": "@_user_1", "name": "本人"}],
        "body": {"content": json.dumps({"text": IMPORTANT}, ensure_ascii=False)},
        "deleted": False, **changes,
    }


def decision(**changes):
    return {
        "importance": "high", "requires_response": True, "summary": IMPORTANT,
        "reasons": ["明确要求本人立即处理持续故障。"],
        "evidence": [{"source": "message", "quote": IMPORTANT}],
        "model": "gpt-6-sol", "reasoning_effort": "xhigh", **changes,
    }


class FakeModel:
    def __init__(self):
        self.calls, self.error, self.callback = [], None, None
        self.result = decision()

    def assess(self, payload):
        self.calls.append(deepcopy(payload))
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return deepcopy(self.result)


class FakeFeishu:
    def __init__(self):
        self.sources, self.sent, self.calls = {}, [], []
        self.read = False
        self.ready = True
        self.tail = {"items": [], "has_more": False}
        self.thread_pages = {None: {"items": [], "has_more": False}}
        self.message_reads, self.list_calls = [], []
        self.send_callback = self.phone_callback = self.read_callback = None
        self.send_error = self.phone_error = None
        self.phone_result = {"invalid_user_id_list": []}
        self.read_checks = 0

    def attention_permissions(self):
        return {"ready": self.ready, "missing_scopes": [] if self.ready else ["im:message:urgent:phone"]}

    def message(self, message_id):
        self.message_reads.append(message_id)
        return deepcopy(self.sources[message_id])

    def message_read_status(self, ids):
        self.read_checks += 1
        if self.read_callback:
            self.read_callback()
        return {message_id: self.read for message_id in ids}

    def messages(self, chat_id, parameters, page_token):
        self.list_calls.append((chat_id, deepcopy(parameters), page_token))
        if parameters["container_id_type"] == "thread":
            assert chat_id == "omt_source"
            assert "start_time" not in parameters and "end_time" not in parameters
            return deepcopy(self.thread_pages[page_token])
        assert chat_id == "oc_team" and parameters["container_id_type"] == "chat"
        return deepcopy(self.tail)

    def send_self_reminder(self, text, uuid):
        self.sent.append((text, uuid))
        if self.send_callback:
            self.send_callback()
        if self.send_error:
            raise self.send_error
        return {"message_id": "om_reminder"}

    def phone_self(self, message_id):
        self.calls.append(message_id)
        if self.phone_callback:
            self.phone_callback()
        if self.phone_error:
            raise self.phone_error
        return deepcopy(self.phone_result)


@pytest.fixture
def system(database, monkeypatch):
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    monkeypatch.setattr(attention, "utcnow", lambda: clock.now)
    settings = SimpleNamespace(
        attention_enabled=True, attention_timeout_seconds=900,
        attention_call_cooldown_seconds=1800, feishu_app_id="cli_test",
        approval_model_timeout_seconds=180,
    )
    sessions = sessionmaker(database, expire_on_commit=False)
    feishu, model = FakeFeishu(), FakeModel()
    with sessions.begin() as db:
        db.add(Account(
            id=1, open_id="ou_owner", tenant_key="tenant", name="本人",
            access_token="unused", scope="", expires_at=clock.now + timedelta(hours=2),
        ))
        db.add(Chat(
            chat_id="oc_team", name="业务群", chat_mode="group", raw={},
            generation="test", visible=True, synced_at=clock.now,
        ))
    service = AttentionService(settings, sessions, feishu, model)
    service.tick()
    return SimpleNamespace(
        service=service, sessions=sessions, feishu=feishu, model=model, clock=clock, settings=settings,
    )


def add_message(system, message_id="om_source", raw=None, created=None):
    system.clock.now += timedelta(seconds=1)
    raw = raw or raw_message(created or system.clock.now, message_id)
    system.feishu.sources[message_id] = deepcopy(raw)
    with system.sessions.begin() as db:
        db.add(Message(
            message_id=message_id, chat_id=raw["chat_id"], msg_type=raw["msg_type"],
            sender_name="发起人", text=IMPORTANT, create_time=int(raw["create_time"]),
            deleted=bool(raw.get("deleted")), raw=raw, synced_at=system.clock.now,
        ))
    return raw


def change_message(system, message_id="om_source", **changes):
    system.clock.now += timedelta(seconds=1)
    raw = {**system.feishu.sources[message_id], **changes}
    system.feishu.sources[message_id] = raw
    with system.sessions.begin() as db:
        row = db.get(Message, message_id)
        row.raw, row.synced_at = deepcopy(raw), system.clock.now
    return raw


def make_due(system, message_id="om_source"):
    with system.sessions() as db:
        row = db.get(MessageAttention, message_id)
        system.clock.now = row.due_at + timedelta(seconds=1)


def row(system, message_id="om_source"):
    with system.sessions() as db:
        return db.get(MessageAttention, message_id)


@pytest.mark.parametrize("change", [
    {"mentions": [{"id": "all", "id_type": "open_id"}]},
    {"mentions": [{"id": "ou_owner", "id_type": "user_id"}]},
    {"mentions": [{"id": "ou_owner"}]},
    {"deleted": True},
    {"sender": {"id": "ou_owner", "id_type": "open_id", "sender_type": "user"}},
    {"sender": {"id": "cli_test", "id_type": "app_id", "sender_type": "app"}},
])
def test_only_explicit_personal_mentions_from_other_senders_qualify(change):
    assert not is_personal_mention(raw_message(utcnow(), **change), "ou_owner", "cli_test")


def test_external_monitoring_bot_mention_can_be_classified():
    raw = raw_message(utcnow(), sender={
        "id": "cli_monitoring", "id_type": "app_id", "sender_type": "app",
    })
    assert is_personal_mention(raw, "ou_owner", "cli_test")


def test_hash_binds_content_but_not_read_counters():
    raw = raw_message(utcnow())
    assert source_hash(raw) == source_hash({**raw, "read_count": 5})
    assert source_hash(raw) != source_hash({**raw, "body": {"content": '{"text":"已恢复"}'}})


@pytest.mark.parametrize("change", [
    {"model": "other"},
    {"reasoning_effort": "low"},
    {"requires_response": "true"},
    {"evidence": []},
    {"evidence": [{"source": "message", "quote": "不存在的故障依据"}]},
    {"evidence": [{"source": "context", "quote": "上下文"}]},
])
def test_unverified_model_result_never_starts_escalation(change):
    payload = {"message": {**raw_message(utcnow()), "text": IMPORTANT}, "context": [{"text": "上下文"}]}
    with pytest.raises(AppError):
        _verified_result(decision(**change), payload)


@pytest.mark.integration
def test_first_activation_does_not_import_historical_messages(system):
    add_message(system, created=system.clock.now - timedelta(minutes=20))
    assert system.service.tick() is None
    assert row(system) is None
    assert not system.model.calls and not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_important_unread_mention_calls_once_after_fifteen_minutes(system):
    add_message(system)
    first = system.service.tick()
    assert first["status"] == "waiting" and not system.feishu.sent
    assert system.service.tick() is None
    make_due(system)

    def assert_durable_marker():
        record = row(system)
        assert record.phone_submitted_at is not None
        with system.sessions() as db:
            assert db.get(AttentionState, 1).last_call_submitted_at == record.phone_submitted_at

    system.feishu.phone_callback = assert_durable_marker
    result = system.service.tick()
    assert result["status"] == "phone_accepted"
    assert system.feishu.calls == ["om_reminder"]
    assert len(system.feishu.sent) == 1
    assert system.feishu.read_checks == 2
    assert row(system).phone_accepted_at
    for _ in range(3):
        system.service.tick()
    assert len(system.feishu.calls) == len(system.model.calls) == 1
    assert "接听" in result["reasons"][0] and "无法" in result["reasons"][0]


@pytest.mark.integration
def test_unchanged_normal_message_is_cached_and_changed_message_is_reassessed(system):
    system.model.result = decision(importance="normal", requires_response=False)
    original = add_message(system)
    assert system.service.tick()["status"] == "normal"
    change_message(system, read_count=5)
    assert system.service.tick() is None
    assert len(system.model.calls) == 1
    change_message(system, body={"content": json.dumps({"text": IMPORTANT + "请接手"}, ensure_ascii=False)})
    system.service.tick()
    assert len(system.model.calls) == 2
    change_message(system, body=original["body"])
    system.service.tick()
    assert len(system.model.calls) == 2  # Returning to a known snapshot uses its cached result.


@pytest.mark.integration
@pytest.mark.parametrize("read,status", [(True, "responded"), (None, "blocked"), ("false", "blocked")])
def test_read_or_unknown_read_state_never_calls(system, read, status):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.read = read
    assert system.service.tick()["status"] == status
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_owner_reply_in_same_chat_suppresses_call_even_outside_thread(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    reply = raw_message(system.clock.now, "om_reply", sender={
        "id": "ou_owner", "id_type": "open_id", "sender_type": "user",
    }, thread_id="unrelated")
    system.feishu.tail = {"items": [reply], "has_more": True, "page_token": "next"}
    assert system.service.tick()["status"] == "responded"
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
@pytest.mark.parametrize("tail", [
    {"items": [], "has_more": True, "page_token": "same-again"},
    {"items": []},  # Missing completion evidence is not assumed to be complete.
    {"items": [{}], "has_more": False},
])
def test_incomplete_or_invalid_tail_blocks_call(system, tail):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.tail = tail
    assert system.service.tick()["status"] == "blocked"
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_reading_source_while_reminder_sends_prevents_phone(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.send_callback = lambda: setattr(system.feishu, "read", True)
    assert system.service.tick()["status"] == "responded"
    assert len(system.feishu.sent) == 1 and not system.feishu.calls
    system.service.tick()
    assert len(system.feishu.sent) == 1


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["read", "send", "model"])
def test_local_acknowledgment_during_network_work_prevents_phone(system, stage):
    add_message(system)

    def callback():
        return system.service.acknowledge("om_source")

    if stage == "model":
        system.model.callback = callback
    first = system.service.tick()
    if stage != "model":
        make_due(system)
        if stage == "read":
            system.feishu.read_callback = callback
        else:
            system.feishu.send_callback = callback
        first = system.service.tick()
    assert first["status"] == "acknowledged" and first["acknowledged"]
    assert not system.feishu.calls


@pytest.mark.integration
def test_model_errors_have_finite_persisted_retry_budget_without_starving_other_messages(system):
    add_message(system)
    system.model.error = TimeoutError("private upstream response")
    first = system.service.tick()
    assert first["status"] == "model_error"
    assert first["error_code"] == "attention_unavailable"
    for attempt in (2, 3):
        system.clock.now = row(system).next_attempt_at
        system.service.tick()
        assert row(system).attempts == attempt
    assert system.service.tick() is None
    add_message(system, "om_second")
    system.model.error = None
    assert system.service.tick()["message_id"] == "om_second"
    assert len(system.model.calls) == 4
    assert not system.feishu.calls


@pytest.mark.integration
@pytest.mark.parametrize("stage,status", [("send", "reminder_unknown"), ("phone", "phone_unknown")])
def test_ambiguous_outbound_requests_are_never_repeated_even_after_restart(system, stage, status):
    add_message(system)
    system.service.tick()
    make_due(system)
    if stage == "send":
        system.feishu.send_error = FeishuError("network")
    else:
        system.feishu.phone_error = FeishuError("network")
    assert system.service.tick()["status"] == status
    service = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    system.clock.now += timedelta(hours=1)
    service.tick()
    assert len(system.feishu.sent) == 1
    assert len(system.feishu.calls) == (1 if stage == "phone" else 0)


@pytest.mark.integration
def test_missing_permissions_can_recover_before_any_outbound_submission(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.ready = False
    assert system.service.tick()["status"] == "blocked_permission"
    assert not row(system).reminder_submitted_at and not system.feishu.sent
    system.feishu.ready = True
    system.clock.now += timedelta(seconds=61)
    assert system.service.tick()["status"] == "phone_accepted"


@pytest.mark.integration
def test_source_edit_after_model_decision_blocks_stale_phone(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.sources["om_source"]["body"] = {"content": '{"text":"问题已解决"}'}
    assert system.service.tick()["status"] == "source_changed"
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_later_resolution_requires_new_model_judgment_and_stops_call(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    resolution = raw_message(system.clock.now, "om_resolution", mentions=[], body={
        "content": json.dumps({"text": "故障已恢复，无需继续处理。"}, ensure_ascii=False),
    })
    system.feishu.tail = {"items": [resolution], "has_more": False}
    assert system.service.tick()["status"] == "queued"
    assert not system.feishu.sent
    system.model.result = decision(
        importance="normal", requires_response=False, summary="故障已经恢复。",
        evidence=[{"source": "context", "quote": "故障已恢复，无需继续处理。"}],
    )
    assert system.service.tick()["status"] == "normal"
    assert len(system.model.calls) == 2
    assert system.model.calls[-1]["context"][-1]["message_id"] == "om_resolution"
    assert system.service.tick() is None
    assert not system.feishu.calls


@pytest.mark.integration
def test_unchanged_fresh_context_is_cached_but_new_progress_is_checked_again(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    followup = raw_message(system.clock.now, "om_followup", mentions=[], body={
        "content": json.dumps({"text": "问题还没有恢复，请尽快接手。"}, ensure_ascii=False),
    })
    system.feishu.tail = {"items": [followup], "has_more": False}
    assert system.service.tick()["status"] == "queued"
    assert system.service.tick()["status"] == "waiting"
    assert len(system.model.calls) == 2

    def resolved_after_reminder():
        system.feishu.tail["items"].append(raw_message(
            system.clock.now + timedelta(seconds=1), "om_resolved", mentions=[],
            body={"content": '{"text":"问题已恢复"}'},
        ))

    system.feishu.send_callback = resolved_after_reminder
    assert system.service.tick()["status"] == "blocked"
    assert len(system.feishu.sent) == 1 and not system.feishu.calls
    assert len(system.model.calls) == 2


@pytest.mark.integration
def test_restart_skips_overdue_unsent_messages_but_keeps_a_visible_record(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    restarted = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    result = restarted.tick()
    assert result["status"] == "skipped_restart"
    assert "未补打" in result["reasons"][0]
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_crash_markers_become_unknown_and_do_not_replay(system):
    add_message(system)
    system.service.tick()
    with system.sessions.begin() as db:
        record = db.get(MessageAttention, "om_source")
        record.reminder_submitted_at = record.phone_submitted_at = system.clock.now
        record.status = "calling"
        record.updated_at = system.clock.now
    system.clock.now += timedelta(seconds=1)
    restarted = AttentionService(system.settings, system.sessions, system.feishu, system.model)
    assert restarted.tick() is None
    assert row(system).status == "phone_unknown"
    assert not system.feishu.calls


@pytest.mark.integration
def test_owner_wide_cooldown_delays_second_phone_and_rechecks_response(system):
    add_message(system)
    system.service.tick()
    add_message(system, "om_second")
    system.service.tick()
    make_due(system)
    assert system.service.tick()["status"] == "phone_accepted"
    system.clock.now += timedelta(seconds=2)
    second = system.service.tick()
    assert second["message_id"] == "om_second" and second["status"] == "waiting"
    assert len(system.feishu.sent) == 1 and len(system.feishu.calls) == 1
    system.feishu.read = True
    system.clock.now = row(system, "om_second").next_attempt_at
    assert system.service.tick()["status"] == "responded"
    assert len(system.feishu.calls) == 1


@pytest.mark.integration
def test_invalid_phone_recipient_result_never_claims_call_accepted(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.feishu.phone_result = {"invalid_user_id_list": ["ou_owner"]}
    assert system.service.tick()["status"] == "phone_unknown"
    assert row(system).phone_accepted_at is None


@pytest.mark.integration
def test_acknowledging_after_submission_preserves_call_audit(system):
    add_message(system)
    system.service.tick()
    make_due(system)
    system.service.tick()
    result = system.service.acknowledge("om_source")
    assert result["phone_submitted_at"] and result["phone_accepted_at"]
    assert not result["can_acknowledge"] and result["acknowledged"]
    assert "无法撤回" in result["reasons"][0]
    with system.sessions() as db:
        assert len(db.scalars(select(MessageAttention)).all()) == 1


@pytest.mark.integration
def test_many_permission_blocked_messages_do_not_starve_a_new_queued_mention(system):
    # Thirteen retries fill a 5-second worker loop before the first 60-second
    # backoff expires. Sorting by status would keep cycling those rows forever.
    created = system.clock.now + timedelta(seconds=1)
    system.clock.now = created + timedelta(minutes=15, seconds=1)
    with system.sessions.begin() as db:
        for index in range(13):
            db.add(MessageAttention(
                message_id=f"om_blocked_{index:02}",
                owner_open_id="ou_owner", chat_id="oc_team", chat_name="业务群",
                text=IMPORTANT, source_created_at=created,
                source_hash=f"{index:064x}", due_at=created + timedelta(minutes=15),
                status="blocked_permission", reasons=["等待电话权限。"],
                model_result=decision(), assessment_cache={},
                next_attempt_at=system.clock.now, updated_at=system.clock.now,
            ))
    system.feishu.ready = False
    system.model.result = decision(importance="normal", requires_response=False)
    add_message(system, "om_new")
    for _ in range(14):
        system.service.tick()
        system.clock.now += timedelta(seconds=5)
    assert row(system, "om_new").status == "normal"
    assert len(system.model.calls) == 1
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
@pytest.mark.parametrize("change", ["edit", "recall"])
def test_changed_previous_context_is_refetched_and_reassessed_before_call(system, change):
    previous = raw_message(system.clock.now, "om_previous", mentions=[], body={
        "content": '{"text":"生产支付服务有持续故障。"}',
    })
    add_message(system, "om_previous", previous)
    add_message(system)
    assert system.service.tick()["status"] == "waiting"
    assert system.model.calls[0]["context"][0]["message_id"] == "om_previous"
    make_due(system)
    if change == "edit":
        system.feishu.sources["om_previous"]["body"] = {
            "content": '{"text":"更正：这是演练，故障已恢复，无需处理。"}',
        }
    else:
        system.feishu.sources["om_previous"]["deleted"] = True
    assert system.service.tick()["status"] == "queued"
    assert "om_previous" in system.feishu.message_reads
    assert not system.feishu.sent and not system.feishu.calls
    system.model.result = decision(importance="normal", requires_response=False)
    assert system.service.tick()["status"] == "normal"
    revised = system.model.calls[-1]["context"]
    if change == "edit":
        assert "更正" in revised[0]["text"]
    else:
        assert revised == []
    assert not system.feishu.calls


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["missing", "wrong_chat", "wrong_id", "changed_time"])
def test_unverifiable_previous_context_blocks_phone(system, invalid):
    previous = raw_message(system.clock.now, "om_previous", mentions=[])
    add_message(system, "om_previous", previous)
    add_message(system)
    system.service.tick()
    make_due(system)
    if invalid == "missing":
        del system.feishu.sources["om_previous"]
    else:
        field, value = {
            "wrong_chat": ("chat_id", "oc_other"),
            "wrong_id": ("message_id", "om_other"),
            "changed_time": ("create_time", str(int(system.clock.now.timestamp() * 1000))),
        }[invalid]
        system.feishu.sources["om_previous"][field] = value
    assert system.service.tick()["status"] == "blocked"
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_context_edit_while_reminder_sends_prevents_phone(system):
    previous = raw_message(system.clock.now, "om_previous", mentions=[])
    add_message(system, "om_previous", previous)
    add_message(system)
    system.service.tick()
    make_due(system)

    def edit_context():
        system.feishu.sources["om_previous"]["body"] = {"content": '{"text":"问题已经解决。"}'}

    system.feishu.send_callback = edit_context
    assert system.service.tick()["status"] == "blocked"
    assert len(system.feishu.sent) == 1 and not system.feishu.calls
    assert system.service.tick() is None


@pytest.mark.integration
def test_shared_model_busy_does_not_exhaust_failure_budget_or_retry_tightly(system):
    add_message(system)
    system.model.error = AppError("attention_model_busy", "模型正忙。", 503)
    for _ in range(5):
        assert system.service.tick()["status"] == "queued"
        current = row(system)
        assert current.attempts == 0
        assert current.next_attempt_at >= system.clock.now + timedelta(seconds=60)
        calls = len(system.model.calls)
        assert system.service.tick() is None
        assert len(system.model.calls) == calls
        system.clock.now = current.next_attempt_at
    system.model.error = None
    assert system.service.tick()["status"] == "waiting"
    assert row(system).attempts == 1
    assert len(system.model.calls) == 6
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
@pytest.mark.parametrize("discover_in_chat", [False, True])
def test_original_thread_reply_suppresses_call_even_if_chat_listing_omits_replies(
    system, discover_in_chat,
):
    raw = raw_message(system.clock.now + timedelta(seconds=1))
    if not discover_in_chat:
        raw["thread_id"] = "omt_source"
    add_message(system, raw=raw)
    system.service.tick()
    make_due(system)
    if discover_in_chat:
        system.feishu.tail["items"] = [{**raw, "thread_id": "omt_source"}]
    reply = raw_message(system.clock.now, "om_thread_reply", sender={
        "id": "ou_owner", "id_type": "open_id", "sender_type": "user",
    }, thread_id="omt_source")
    system.feishu.thread_pages = {
        None: {"items": [raw], "has_more": True, "page_token": "second"},
        "second": {"items": [reply], "has_more": False},
    }
    assert system.service.tick()["status"] == "responded"
    thread_reads = [call for call in system.feishu.list_calls if call[0] == "omt_source"]
    assert len(thread_reads) == 2
    assert not system.feishu.sent and not system.feishu.calls


@pytest.mark.integration
def test_new_progress_in_source_thread_requires_new_model_judgment(system):
    raw = raw_message(system.clock.now + timedelta(seconds=1), thread_id="omt_source")
    add_message(system, raw=raw)
    system.service.tick()
    make_due(system)
    resolution = raw_message(system.clock.now, "om_resolved", mentions=[], thread_id="omt_source", body={
        "content": '{"text":"故障已经恢复，无需继续处理。"}',
    })
    system.feishu.thread_pages = {None: {"items": [raw, resolution], "has_more": False}}
    assert system.service.tick()["status"] == "queued"
    assert not system.feishu.sent
    system.model.result = decision(
        importance="normal", requires_response=False,
        evidence=[{"source": "context", "quote": "故障已经恢复，无需继续处理。"}],
    )
    assert system.service.tick()["status"] == "normal"
    assert system.model.calls[-1]["context"][-1]["message_id"] == "om_resolved"
    assert not system.feishu.calls


@pytest.mark.integration
def test_incomplete_source_thread_pagination_blocks_phone_after_ten_pages(system):
    raw = raw_message(system.clock.now + timedelta(seconds=1), thread_id="omt_source")
    add_message(system, raw=raw)
    system.service.tick()
    make_due(system)
    system.feishu.thread_pages = {
        None if i == 0 else f"page-{i}": {
            "items": [], "has_more": True, "page_token": f"page-{i + 1}",
        } for i in range(10)
    }
    result = system.service.tick()
    assert result["status"] == "blocked"
    assert result["error_code"] == "attention_tail_incomplete"
    assert len([call for call in system.feishu.list_calls if call[0] == "omt_source"]) == 10
    assert not system.feishu.sent and not system.feishu.calls
