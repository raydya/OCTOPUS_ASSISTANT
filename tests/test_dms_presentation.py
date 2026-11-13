"""DMS review facts update the existing notice without changing approval status."""

from copy import deepcopy

import pytest
from test_status_cards import button, content, dms

from app.status_notice_content import (
    attention_notice,
    card_fingerprint,
    compact_text,
    dms_received_notice,
    status_card,
)


def review(**changes):
    return {
        "status": "ready", "risk": "low", "summary": "测试库单行更新，预检通过，已提供回滚。",
        "reasons": ["测试环境中影响一行数据。", "SQL 预检通过且有回滚语句。"],
        "missing_evidence": [],
        "facts": {"申请人": "测试申请人", "环境": "test", "数据库": "sandbox",
                  "变更类型": "UPDATE", "预计影响行数": "1", "回滚": "已提供"},
        "fetched_at": "2026-11-13T10:00:00Z", **changes,
    }


def test_low_risk_card_shows_change_summary_facts_and_reasons_but_remains_pending():
    payload = attention_notice(dms(dms_review=review()))
    rendered = status_card(payload)
    visible = "\n".join(content(rendered))
    assert "🔔 待你审批" in visible
    assert "风险判断：低危" in visible and "详情概要：测试库单行更新" in visible
    assert "申请人：测试申请人" in visible and "数据库：sandbox" in visible
    assert "预计影响行数：1" in visible and "判断依据：测试环境中影响一行数据" in visible
    assert "已通过" not in visible
    assert payload["phase"] == "needs_human" and payload["terminal"] is False
    assert "风险判断：低危" in compact_text(payload)
    assert len([element for element in rendered["elements"] if element["tag"] == "action"]) == 1
    assert "value" not in button(rendered)


@pytest.mark.parametrize("risk,status,missing,expected", [
    ("high", "ready", [], "风险判断：高危"),
    ("high", "ready", ["生产影响尚未完整核实"], "风险判断：高危（信息不足，待核查）"),
    ("high", "unavailable", ["详情暂时不可读取"], "风险判断：高危（信息不足，待核查）"),
    ("low", "unavailable", [], "风险判断：高危（信息不足，待核查）"),
    ("low", "ready", ["影响行数未知"], "风险判断：高危（信息不足，待核查）"),
])
def test_missing_or_inconsistent_review_cannot_display_low_risk(risk, status, missing, expected):
    payload = attention_notice(dms(dms_review=review(
        risk=risk, status=status, missing_evidence=missing,
    )))
    visible = content(status_card(payload))
    assert expected in visible
    if missing:
        assert "待核查：" + "；".join(missing) in visible


def test_pending_details_and_receipt_do_not_display_a_stale_low_risk_claim():
    payload = attention_notice(dms(dms_review=review(status="pending")))
    receipt = dms_received_notice(payload, chat_id="oc_dba")
    for notice in (payload, receipt):
        visible = "\n".join(content(status_card(notice)))
        assert "正在调取 DMS 工单详情，风险待判断" in visible
        assert "低危" not in visible and "高危" not in visible
        assert "sandbox" not in visible


def test_review_change_updates_semantic_revision_but_read_time_does_not():
    original = attention_notice(dms(dms_review=review()))
    later_read = attention_notice(dms(dms_review=review(fetched_at="2026-11-13T10:05:00Z")))
    changed = attention_notice(dms(dms_review=review(
        risk="high", summary="生产库数据更新，需要核实业务影响。",
        reasons=["目标为生产环境。"], facts={"环境": "prod"},
    )))
    assert original["revision"] == later_read["revision"]
    assert card_fingerprint(original) == card_fingerprint(later_read)
    assert original["revision"] != changed["revision"]
    assert card_fingerprint(original) != card_fingerprint(changed)
    assert original["source_id"] == changed["source_id"]


def test_only_summary_fields_enter_plain_text_cards_and_original_review_is_untouched():
    assessment = review(
        summary="<b>单行更新</b> https://example.invalid/private",
        ExeSQL="update users set password='secret'", token="secret-token",
        facts={"环境": "test", "ExeSQL": "secret-sql", "password": "secret-password"},
        source_url="https://dms.aliyun.com/?orderId=28562578",
    )
    before = deepcopy(assessment)
    payload = attention_notice(dms(dms_review=assessment))
    rendered = status_card(payload)
    visible = "\n".join(content(rendered))
    assert "<b>单行更新</b>" in visible
    assert "secret" not in visible and "http" not in visible
    assert all(element["text"]["tag"] == "plain_text" for element in rendered["elements"] if element["tag"] == "div")
    assert button(rendered)["url"] == "https://dms.aliyun.com/?orderId=28562578"
    assert assessment == before


def test_rich_receipt_retains_completed_review_without_replacing_receipt_identity():
    pending = attention_notice(dms(dms_review=review(status="pending")))
    ready = attention_notice(dms(dms_review=review()))
    received_pending = dms_received_notice(pending, chat_id="oc_dba")
    received_ready = dms_received_notice(ready, chat_id="oc_dba")
    assert received_pending["revision"] == received_ready["revision"]
    assert "风险判断：低危" in content(status_card(received_ready))
    assert "风险判断：低危" in received_ready["text"]


def test_dms_review_is_not_projected_onto_an_unrelated_message():
    payload = attention_notice(dms(source_type="message", dms_review=review()))
    assert not any("风险判断" in line for line in content(status_card(payload)))
