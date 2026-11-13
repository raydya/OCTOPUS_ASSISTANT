"""Enrichment binds remote reads to the current verified DMS card and owner."""

import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_dms_attention import APP_ID, CHAT_ID, card

from app import dms_review
from app.dms_attention import _metadata, _parse, dms_attention_id
from app.dms_review import REFRESH_SECONDS, DmsReviewService, order_reference, review_view
from app.errors import AppError
from app.models import Account, Chat, DmsReview, Message, MessageAttention, utcnow
from app.status_notice_content import compact_text
from app.status_notifications import StatusNotificationService


class DetailReader:
    def __init__(self):
        self.calls, self.before, self.error = [], None, None
        self.snapshot = {
            "order_id": "123456", "base": {"OrderId": 123456, "PluginType": "DATA_EXPORT"},
            "detail": {}, "sql_checks": [], "complete": True, "errors": [],
        }

    def fetch(self, identifier):
        self.calls.append(identifier)
        if self.before:
            self.before()
        if self.error:
            raise self.error
        return deepcopy(self.snapshot)


@pytest.fixture
def enriched(database, settings, monkeypatch):
    clock = SimpleNamespace(now=utcnow().replace(microsecond=0))
    monkeypatch.setattr(dms_review, "utcnow", lambda: clock.now)
    settings.dms_sender_app_id, settings.dms_chat_id = APP_ID, CHAT_ID
    settings.dms_details_enabled = True
    sessions = sessionmaker(database, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(Account(
            id=1, open_id="ou_owner", tenant_key="tenant", name="本人",
            access_token="unused", scope="", expires_at=clock.now + timedelta(hours=2),
        ))
        db.add(Chat(
            chat_id=CHAT_ID, name="DMS", chat_mode="p2p", raw={},
            generation="test", visible=True, synced_at=clock.now,
        ))
    seen = []

    def assess(snapshot):
        # Risk policy has its own tests; here the assertion is that only the
        # current, identity-bound snapshot reaches assessment and publication.
        seen.append(deepcopy(snapshot))
        complete = snapshot.get("complete") is True and not snapshot.get("errors")
        return {
            "status": "ready" if complete else "unavailable", "risk": "low" if complete else "high",
            "summary": "测试详情概要" if complete else "详情身份或完整性尚未核实。",
            "reasons": ["已读取并校验测试详情。"], "facts": {"数据库": "sandbox"},
            "missing_evidence": [] if complete else ["完整详情与明确的工单引用"],
        }

    monkeypatch.setattr(dms_review, "assess_dms_detail", assess)
    reader = DetailReader()
    return SimpleNamespace(
        settings=settings, sessions=sessions, clock=clock, reader=reader, assessed=seen,
        service=DmsReviewService(settings, sessions, reader),
    )


def put_card(system, identifier="om_dms", *, raw=None, url=None, buttons=("同意", "拒绝")):
    system.clock.now += timedelta(seconds=1)
    if raw is None:
        raw = card(system.clock.now, identifier, buttons=buttons)
        body = json.loads(raw["body"]["content"])
        body["card_link"]["url"] = url or "https://dms.aliyun.com/?orderId=123456"
        raw["body"] = {"content": json.dumps(body, ensure_ascii=False)}
    with system.sessions.begin() as db:
        row = db.get(Message, identifier)
        if row is None:
            row = Message(
                message_id=identifier, chat_id=raw["chat_id"], msg_type=raw["msg_type"],
                sender_name="DBA", text="DMS", create_time=int(raw["create_time"]),
            )
            db.add(row)
        row.raw, row.deleted, row.synced_at = deepcopy(raw), bool(raw.get("deleted")), system.clock.now
    return raw


def records(system):
    with system.sessions() as db:
        return db.scalars(select(DmsReview).order_by(DmsReview.source_id)).all()


def view(system, raw, *, owner="ou_owner"):
    parsed = _parse(raw)
    identifier = dms_attention_id(APP_ID, CHAT_ID, parsed["task_key"], owner)
    with system.sessions() as db:
        return review_view(db, system.settings, owner, identifier, parsed)


@pytest.mark.integration
@pytest.mark.parametrize("change", ["sender", "tenant", "group", "text", "deleted", "unsafe_link"])
def test_only_trusted_pending_cards_are_queried(enriched, change):
    system = enriched
    raw = card(system.clock.now)
    if change == "sender":
        raw["sender"]["id"] = "cli_other"
    elif change == "tenant":
        raw["sender"]["tenant_key"] = "other_tenant"
    elif change == "group":
        raw["chat_type"] = "group"
    elif change == "text":
        raw["msg_type"] = "text"
    elif change == "deleted":
        raw["deleted"] = True
    else:
        body = json.loads(raw["body"]["content"])
        body["card_link"]["url"] = "https://example.invalid/?orderId=123456"
        raw["body"]["content"] = json.dumps(body)
    put_card(system, raw=raw)
    assert system.service.tick() is None
    assert not system.reader.calls and not records(system)


@pytest.mark.integration
def test_duplicate_cards_share_cached_review_and_do_not_repeat_immediate_reads(enriched):
    system = enriched
    raw = put_card(system)
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    first = records(system)[0]
    duplicate = put_card(system, "om_dms_duplicate")
    for _ in range(3):
        assert system.service.tick() is None
    current = records(system)[0]
    assert len(records(system)) == 1
    assert current.source_id == first.source_id and current.source_message_id == "om_dms_duplicate"
    assert current.fetched_at == first.fetched_at and current.next_attempt_at == first.next_attempt_at
    assert view(system, raw)["risk"] == view(system, duplicate)["risk"] == "low"
    assert system.reader.calls == ["123456"]


@pytest.mark.integration
@pytest.mark.parametrize("change", ["content", "owner", "terminal", "sender", "deleted", "stored_chat"])
def test_state_changes_during_remote_read_discard_stale_result(enriched, change):
    system = enriched
    original = put_card(system)

    def interfere():
        if change == "owner":
            with system.sessions.begin() as db:
                db.get(Account, 1).open_id = "ou_rebound"
            return
        if change == "stored_chat":
            with system.sessions.begin() as db:
                db.add(Chat(
                    chat_id="oc_other", name="Other", chat_mode="p2p", raw={},
                    generation="test", visible=True, synced_at=system.clock.now,
                ))
                db.flush()
                db.get(Message, "om_dms").chat_id = "oc_other"
            return
        fresh = deepcopy(original)
        fresh["update_time"] = str(int((system.clock.now + timedelta(seconds=1)).timestamp() * 1000))
        if change == "sender":
            fresh["sender"]["id"] = "cli_other"
        elif change == "deleted":
            fresh["deleted"] = True
        else:
            body = json.loads(fresh["body"]["content"])
            if change == "content":
                body["elements"][0][0]["text"] = "变更范围已扩大，等待重新审批。"
            else:
                body["elements"] = [[{"tag": "button", "text": "已同意"}]]
            fresh["body"]["content"] = json.dumps(body, ensure_ascii=False)
        put_card(system, raw=fresh)

    system.reader.before = interfere
    assert system.service.tick() == {"status": "discarded"}
    record = records(system)[0]
    assert record.result == {} and record.fetched_at is None
    assert system.reader.calls == ["123456"]


@pytest.mark.integration
def test_source_change_hides_cached_low_result_until_new_detail_read(enriched):
    system = enriched
    original = put_card(system)
    system.service.tick()
    assert view(system, original)["risk"] == "low"
    fresh = deepcopy(original)
    fresh["update_time"] = str(int((system.clock.now + timedelta(seconds=1)).timestamp() * 1000))
    body = json.loads(fresh["body"]["content"])
    body["elements"][0][0]["text"] = "本次新增另一张表的变更。"
    fresh["body"]["content"] = json.dumps(body, ensure_ascii=False)
    put_card(system, raw=fresh)
    assert view(system, fresh)["status"] == "pending"
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    assert system.reader.calls == ["123456", "123456"]


@pytest.mark.integration
def test_terminal_card_stops_further_reads_and_hides_previous_risk(enriched):
    system = enriched
    put_card(system)
    system.service.tick()
    done = put_card(system, buttons=("已同意",))
    assert view(system, done) is None
    system.clock.now += timedelta(seconds=REFRESH_SECONDS)
    assert system.service.tick() is None
    assert not records(system)[0].active
    assert system.reader.calls == ["123456"]


@pytest.mark.integration
def test_reader_failure_is_high_risk_without_exception_contents_and_retries(enriched):
    system = enriched
    raw = put_card(system)
    system.reader.error = RuntimeError("secret connection details must not escape")
    assert system.service.tick() == {"status": "unavailable", "risk": "high"}
    failed = records(system)[0]
    assert failed.result["error_code"] == "dms_details_unavailable"
    assert "secret" not in json.dumps(failed.result)
    assert view(system, raw)["risk"] == "high"
    assert system.service.tick() is None
    system.clock.now = failed.next_attempt_at
    system.reader.error = None
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    assert system.reader.calls == ["123456", "123456"]


@pytest.mark.integration
@pytest.mark.parametrize("identity", ["conflicting_link", "unverified_task", "different_response", "different_type", "different_base"])
def test_uncertain_order_identity_never_becomes_low_risk(enriched, identity):
    system = enriched
    raw = put_card(system, url={
        "conflicting_link": "https://dms.aliyun.com/?orderId=999999",
        "unverified_task": "https://dms.aliyun.com/?taskId=123456",
        "different_response": "https://dms.aliyun.com/?orderId=123456",
        "different_type": "https://dms.aliyun.com/?orderId=123456",
        "different_base": "https://dms.aliyun.com/?orderId=123456",
    }[identity])
    if identity == "different_response":
        system.reader.snapshot["order_id"] = "999999"
    elif identity == "different_type":
        system.reader.snapshot["base"]["PluginType"] = "DC_COMMON"
    elif identity == "different_base":
        system.reader.snapshot["base"]["OrderId"] = 999999
    assert system.service.tick() == {"status": "unavailable", "risk": "high"}
    assert view(system, raw)["risk"] == "high"
    if identity == "conflicting_link":
        assert not system.reader.calls
    elif identity == "unverified_task":
        assert system.assessed[-1]["complete"] is False
        assert "dms_order_reference_unverified" in system.assessed[-1]["errors"]
        assert system.assessed[-1]["detail"] == {} and system.assessed[-1]["sql_checks"] == []
    elif identity == "different_type":
        assert system.assessed[-1]["errors"] == ["dms_order_type_unverified"]
        assert system.assessed[-1]["detail"] == {}


@pytest.mark.integration
def test_initial_discovery_includes_existing_pending_card_older_than_recent_cursor(enriched):
    system = enriched
    raw = put_card(system)
    parsed = _parse(raw)
    identifier = dms_attention_id(APP_ID, CHAT_ID, parsed["task_key"], "ou_owner")
    old = system.clock.now - timedelta(days=3)
    with system.sessions.begin() as db:
        db.get(Message, "om_dms").synced_at = old
        db.add(MessageAttention(
            message_id=identifier, source_type="dms_card", owner_open_id="ou_owner",
            chat_id=CHAT_ID, chat_name="DMS", text=parsed["task_key"],
            source_data=_metadata(parsed, APP_ID, CHAT_ID), source_hash=parsed["hash"],
            source_created_at=old, due_at=old + timedelta(minutes=15), status="waiting",
        ))
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    assert system.reader.calls == ["123456"]


@pytest.mark.integration
def test_status_notice_can_show_review_before_attention_loop_creates_its_row(enriched):
    system = enriched
    raw = put_card(system)
    notifications = StatusNotificationService(system.settings, system.sessions, None)

    def notice():
        with system.sessions() as db:
            assert db.scalar(select(MessageAttention)) is None
            return notifications._dms_message(db, db.get(Message, "om_dms"), db.get(Chat, CHAT_ID), "ou_owner")

    pending = notice()
    assert "风险待判断" in compact_text(pending)
    system.service.tick()
    assessed = notice()
    assert "风险判断：低危" in compact_text(assessed)
    assert "详情概要：测试详情概要" in compact_text(assessed)
    assert pending["source_id"] == assessed["source_id"] and pending["revision"] != assessed["revision"]
    assert view(system, raw)["status"] == "ready"


@pytest.mark.integration
def test_expired_result_is_hidden_and_revalidated(enriched):
    system = enriched
    raw = put_card(system)
    system.service.tick()
    system.clock.now += timedelta(seconds=REFRESH_SECONDS)
    assert view(system, raw)["status"] == "pending"
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    assert len(system.reader.calls) == 2


@pytest.mark.integration
def test_message_reused_for_different_task_retires_old_review(enriched):
    system = enriched
    original = put_card(system)
    system.service.tick()
    due = records(system)[0].next_attempt_at
    changed = deepcopy(original)
    body = json.loads(changed["body"]["content"])
    body["title"] = "DMS-数据导出-654321"
    body["card_link"]["url"] = "https://dms.aliyun.com/?orderId=654321"
    changed["body"]["content"] = json.dumps(body)
    put_card(system, raw=changed)
    system.reader.snapshot["order_id"] = "654321"
    system.reader.snapshot["base"]["OrderId"] = 654321
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    system.clock.now = due
    assert system.service.tick() is None
    old = next(record for record in records(system) if record.task_key.endswith("-123456"))
    assert not old.active and not old.result
    assert system.reader.calls == ["123456", "654321"]


@pytest.mark.parametrize("url", [
    "https://dms.aliyun.com/?orderId=123456",
    "https://dms.aliyun.com/#/ticket?orderId=123456",
    "https://dms.aliyun.com/#ticketId=123456",
])
def test_explicit_order_references_are_verified_in_query_or_hash_routes(url):
    raw = card(utcnow())
    body = json.loads(raw["body"]["content"])
    body["card_link"]["url"] = url
    raw["body"]["content"] = json.dumps(body)
    assert order_reference(raw, _parse(raw)) == ("123456", True)


@pytest.mark.parametrize("path", ["/console/mobile/index/", "/console/mobile/index"])
def test_verified_mobile_link_pid_identifies_the_order(path):
    raw = card(utcnow())
    body = json.loads(raw["body"]["content"])
    body["card_link"]["url"] = f"https://dms.aliyun.com{path}?pid=123456&insId=789&notify=false&imType=Lark"
    raw["body"]["content"] = json.dumps(body)
    assert order_reference(raw, _parse(raw)) == ("123456", True)


@pytest.mark.parametrize("url", [
    "https://dms.aliyun.com/?pid=123456",
    "https://dms.aliyun.com/console/mobile/index/other?pid=123456",
    "https://example.invalid/console/mobile/index/?pid=123456",
    "http://dms.aliyun.com/console/mobile/index/?pid=123456",
])
def test_pid_outside_verified_official_mobile_route_is_not_an_order_reference(url):
    raw = card(utcnow())
    body = json.loads(raw["body"]["content"])
    body["card_link"]["url"] = url
    raw["body"]["content"] = json.dumps(body)
    assert order_reference(raw, _parse(raw)) == ("123456", False)


@pytest.mark.parametrize("query", [
    "pid=999999", "pid=123456&orderId=999999", "pid=123456#ticketId=999999",
    "pid=123456&pid=999999",
])
def test_mobile_pid_must_match_title_and_other_order_references(query):
    raw = card(utcnow())
    body = json.loads(raw["body"]["content"])
    body["card_link"]["url"] = "https://dms.aliyun.com/console/mobile/index/?" + query
    raw["body"]["content"] = json.dumps(body)
    with pytest.raises(AppError):
        order_reference(raw, _parse(raw))


@pytest.mark.integration
def test_english_data_correct_card_uses_verified_change_order_type(enriched):
    system = enriched
    raw = card(system.clock.now)
    body = json.loads(raw["body"]["content"])
    body["title"] = "DMS-Data Correct-123456"
    body["card_link"]["url"] = "https://dms.aliyun.com/console/mobile/index/?pid=123456&insId=789"
    raw["body"]["content"] = json.dumps(body)
    system.reader.snapshot["base"]["PluginType"] = "DC_COMMON"
    put_card(system, raw=raw)
    assert system.service.tick() == {"status": "ready", "risk": "low"}
    assert system.reader.calls == ["123456"]
    assert system.assessed[-1]["base"]["PluginType"] == "DC_COMMON"


def test_conflicting_order_query_and_fragment_are_rejected():
    raw = card(utcnow())
    body = json.loads(raw["body"]["content"])
    body["card_link"]["url"] = "https://dms.aliyun.com/?orderId=123456#ticketId=999999"
    raw["body"]["content"] = json.dumps(body)
    with pytest.raises(AppError, match="标识不一致"):
        order_reference(raw, _parse(raw))
