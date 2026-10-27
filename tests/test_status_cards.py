"""Display-only cards remain compact without changing legacy business revisions."""

from copy import deepcopy
from types import SimpleNamespace as N
from urllib.parse import parse_qs, urlsplit

import pytest

from app import status_cards
from app.status_notice_content import (
    approval_notice,
    attention_notice,
    card_fingerprint,
    compact_text,
    dms_received_notice,
    mention_received_notice,
    status_card,
)

BASE = "http://localhost:8180"


def task(**changes):
    return N(**{
        "task_id": "task_one", "instance_code": "instance_one", "title": "服务发布申请",
        "topic": "1", "status": "1", "visible": True, "initiator_name": "测试发起人",
        "raw": {"user_id": "ou_owner"}, **changes,
    })


def review(**changes):
    return N(**{
        "task_id": "task_one", "instance_code": "instance_one", "owner_open_id": "ou_owner",
        "execution_status": "awaiting_review", "decision": "needs_review", "reasons": [],
        "model_review": {}, **changes,
    })


def dms(**changes):
    return N(**{
        "message_id": "dms_task_one", "source_type": "dms_card", "chat_id": "oc_dba",
        "status": "waiting", "acknowledged_at": None,
        "source_data": {
            "task_key": "DMS-权限申请-28562578", "task_kind": "权限申请", "task_id": "28562578",
            "card_status": "pending",
        },
        "model_result": {}, "reasons": [], "text": "DMS-权限申请-28562578", **changes,
    })


def mention(**changes):
    return N(**{
        "message_id": "om_message", "source_type": "message", "chat_id": "oc_group",
        "chat_name": "平台研发群", "status": "normal", "acknowledged_at": None,
        "model_result": {"requires_response": False}, "reasons": [], "text": "请确认部署状态",
        **changes,
    })


def content(card):
    return [
        card["header"]["title"]["content"],
        *(element["text"]["content"] for element in card["elements"] if element["tag"] == "div"),
    ]


def button(card):
    return next(element["actions"][0] for element in card["elements"] if element["tag"] == "action")


def test_dms_card_updates_only_state_and_retains_one_source_button():
    pending = attention_notice(dms(), base_url=BASE)
    received = dms_received_notice(pending, base_url=BASE, chat_id="oc_dba")
    approved = attention_notice(dms(
        status="responded", source_data={
            "task_key": "DMS-权限申请-28562578", "task_kind": "权限申请",
            "task_id": "28562578", "card_status": "approved",
        },
    ), base_url=BASE)
    for payload, expected in (
        (received, "⏳ 正在核验"), (pending, "🔔 待你审批"), (approved, "✅ 已通过"),
    ):
        rendered = status_card(payload)
        assert content(rendered) == ["DMS 权限申请 #28562578", expected]
        assert len(rendered["elements"]) == 2
        assert button(rendered)["text"]["content"] == "查看原会话"
        assert rendered["config"]["update_multi"] is True
        assert not any(word in compact_text(payload) for word in ("依据", "下一步", "边界", "http", "工作台"))


@pytest.mark.parametrize("execution,confirmed,expected", [
    ("awaiting_review", None, "⏳ 待提交审批"),
    ("submitting", None, "⏳ 正在提交，结果未确认"),
    ("unknown", None, "⚠️ 提交结果未确认"),
    ("approved", None, "⚠️ 提交结果未确认"),
    ("approved", "confirmed", "✅ 已通过"),
])
def test_approval_card_never_confuses_proposal_or_submission_with_confirmation(execution, confirmed, expected):
    payload = approval_notice(task(), review(
        execution_status=execution, confirmed_at=confirmed, decision="eligible",
    ), base_url=BASE)
    assert content(status_card(payload)) == ["服务发布申请", expected]


def test_completed_model_approve_proposal_is_still_pending_submission():
    model = {
        "status": "complete", "decision": "approve", "eligible": True,
        "assessment_id": "assessment_one", "snapshot_hash": "a" * 64,
        "summary": "可以通过", "reasons": ["解释" * 100],
    }
    payload = approval_notice(task(), review(
        model_review=model, assessment_id="assessment_one", snapshot_hash="a" * 64,
    ), base_url=BASE, current_snapshot_hash="a" * 64)
    assert payload["phase"] == "waiting"
    assert content(status_card(payload)) == ["服务发布申请", "⏳ 待提交审批"]
    assert "已通过" not in compact_text(payload)


def test_approval_button_uses_documented_instance_code_and_device_specific_paths():
    action = button(status_card(approval_notice(task())))
    assert action["text"]["content"] == "查看申请" and "url" not in action
    links = action["multi_url"]
    mobile, desktop = parse_qs(urlsplit(links["url"]).query), parse_qs(urlsplit(links["pc_url"]).query)
    assert mobile["appId"] == [status_cards.APPROVAL_APP_ID]
    assert mobile["path"] == ["pages/detail/index?instanceId=instance_one"]
    assert desktop["path"] == ["pc/pages/in-process/index?instanceId=instance_one"]
    assert desktop["mode"] == ["appCenter"]
    assert links["ios_url"] == links["android_url"] == links["url"]


@pytest.mark.parametrize("state,expected", [
    ("approved", "✅ 已通过"), ("rejected", "❌ 已拒绝"), ("cancelled", "↩️ 已撤销"),
    ("unknown", "ℹ️ 已停止提醒，处理状态未确认"),
])
def test_dms_terminal_card_uses_verified_card_state(state, expected):
    row = dms(status="responded")
    row.source_data["card_status"] = state
    assert content(status_card(attention_notice(row)))[1] == expected


def test_stopping_a_dms_reminder_does_not_claim_approval():
    assert content(status_card(attention_notice(dms(acknowledged_at="now"))))[1] == "⏸ 已停止提醒"


@pytest.mark.parametrize("requires_response,expected", [
    (False, "ℹ️ 无需响应"), (True, "💬 普通消息，仍需回复"), (None, "ℹ️ 无需电话提醒"),
])
def test_normal_mentions_distinguish_reply_requirement(requires_response, expected):
    payload = attention_notice(mention(model_result={"requires_response": requires_response}))
    assert content(status_card(payload)) == ["@ 我 · 平台研发群", expected]
    assert button(status_card(payload))["text"]["content"] == "查看原会话"


def test_error_or_intervention_has_at_most_one_short_sentence():
    payload = approval_notice(task(), review(reasons=[
        "请确认服务负责人。还需要补齐其他很多内容。", "这是另一条完整原因。",
    ]))
    assert content(status_card(payload)) == ["服务发布申请", "🔔 待你处理", "请确认服务负责人。"]
    blocked = attention_notice(mention(status="blocked", reasons=[
        "原消息已变化。需要重新核验。", "请查看工作台 http://localhost:8180",
    ]))
    assert content(status_card(blocked))[-1] == "原消息已变化。"


def test_model_question_takes_precedence_over_long_reasons():
    model = {
        "status": "complete", "decision": "needs_human", "eligible": False,
        "assessment_id": "assessment_one", "snapshot_hash": "a" * 64,
        "questions": ["迁移已验收吗？还有第二个问题？"], "reasons": ["很多依据" * 100],
    }
    payload = approval_notice(task(), review(
        model_review=model, assessment_id="assessment_one", snapshot_hash="a" * 64,
    ), current_snapshot_hash="a" * 64)
    assert content(status_card(payload))[-1] == "迁移已验收吗？"


def test_dms_application_link_is_allowed_only_from_validated_dms_host():
    direct = attention_notice(dms(), source_url="https://dms.aliyun.com/?ticketId=28562578")
    assert button(status_card(direct))["text"]["content"] == "查看申请"
    assert button(status_card(direct))["url"] == "https://dms.aliyun.com/?ticketId=28562578"
    other = attention_notice(dms(), source_url="https://example.invalid/collect")
    assert button(status_card(other))["text"]["content"] == "查看原会话"
    assert direct["revision"] == other["revision"]
    assert card_fingerprint(direct) != card_fingerprint(other)


def test_card_is_plain_text_without_callback_action_or_exposed_long_links():
    payload = attention_notice(mention(
        chat_name="<b>平台群</b>", status="blocked",
        reasons=["请先核验权限。详细说明 https://example.invalid/very/long/url"],
    ))
    rendered = status_card(payload)
    assert rendered["header"]["title"]["tag"] == "plain_text"
    assert all(element["text"]["tag"] == "plain_text" for element in rendered["elements"] if element["tag"] == "div")
    assert "value" not in button(rendered) and "callback" not in str(rendered)
    assert all("http" not in value for value in content(rendered))
    assert "http" not in compact_text(payload)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://localhost:8180", "https://example.invalid/", "https://dms.aliyun.com@evil.invalid/"])
def test_malformed_persisted_button_never_becomes_an_action(url):
    payload = attention_notice(dms())
    payload["card"]["button"] = {"label": "查看申请", "url": url}
    assert not any(element["tag"] == "action" for element in status_card(payload)["elements"])


def test_card_projection_preserves_historical_business_revision_golden_values():
    approval = approval_notice(task(), base_url=BASE)
    pending = attention_notice(dms(), base_url=BASE)
    receipt = dms_received_notice(pending, base_url=BASE, chat_id="oc_dba")
    received = mention_received_notice(mention(), N(name="平台研发群"), base_url=BASE)
    assert [item["revision"] for item in (approval, pending, receipt, received)] == [
        "fd5b1a6823f8134cb07a1163843977b10bf7ec92e1bc205bed7956540faa206c",
        "d13cf5f575adf059ed4709519d2bf7f0ec3ba2fb6c2d1df5a740108172bc681b",
        "75c864b1396a8a84ef9de11b9684a4324f6ab18331dfd33fae7fb422620c7044",
        "aa798a945e76f4f27df76967ab64884e1eff84a5e9b6bc91d503273c10debc35",
    ]


def test_rendering_is_pure_and_format_hash_is_separate_from_business_revision(monkeypatch):
    payload = attention_notice(dms())
    before = deepcopy(payload)
    digest = card_fingerprint(payload)
    assert status_card(payload) == status_card(payload)
    assert payload == before
    monkeypatch.setattr(status_cards, "FORMAT_VERSION", 2)
    assert card_fingerprint(payload) != digest
    assert payload["revision"] == before["revision"]


def test_legacy_queued_payload_has_safe_compact_fallback():
    payload = attention_notice(dms())
    payload.pop("card")
    rendered = status_card(payload)
    assert content(rendered) == ["DMS 权限申请 #28562578", "🔔 待你处理"]
    assert button(rendered)["text"]["content"] == "查看原会话"
