import json
from copy import deepcopy

import pytest

from scripts import tls_report_analysis as analysis


@pytest.fixture
def payload():
    groups = [
        {"id": "group-sql", "service": "pay-service", "count": 9,
         "logger": "PaymentRepository", "exception": "SQLException",
         "summary": "Unknown column 'billing_mode' in field list",
         "examples": ["SQLException: Unknown column 'billing_mode' in field list"],
         "first_time": 1000, "last_time": 2000},
        {"id": "group-timeout", "service": "account-service", "count": 4,
         "logger": "AccountClient", "exception": "SocketTimeoutException",
         "summary": "Read timed out contacting downstream",
         "examples": ["SocketTimeoutException: Read timed out contacting downstream"],
         "first_time": 1200, "last_time": 2200},
    ]
    return analysis.source_payload(groups, {"status": "partial", "detail": "5xx samples unavailable"}, scope={
        "project": "pro-jd-sh-service", "start": "2026-10-20T16:00:00+08:00",
        "end": "2026-10-21T16:00:00+08:00",
    })


def finding(group_id="group-sql", quote="Unknown column 'billing_mode'", **changes):
    result = {
        "title": "字段缺失", "priority": "P2", "group_ids": [group_id],
        "direct_cause": "SQL 引用了缺失字段。", "root_cause": "数据库表结构不含 billing_mode 字段。",
        "cause_status": "confirmed", "actions": ["核对迁移版本与当前表结构，确认后补齐兼容迁移。"],
        "evidence": [{"source_id": f"log:{group_id}", "quote": quote}],
    }
    result.update(changes)
    return result


def model_result():
    return {
        "summary": "发现 SQL 字段缺失与下游读取超时。",
        "findings": [finding(), finding("group-timeout", "Read timed out contacting downstream",
                                        title="下游读取超时", cause_status="unverified",
                                        root_cause="日志仅证明读取超时，尚不能确认对端故障。")],
        "model": "gpt-6-sol", "reasoning_effort": "xhigh", "usage": {"input_tokens": 100},
    }


def test_valid_evidence_and_exact_group_coverage_are_accepted(payload):
    result = model_result()
    assert analysis.validate_analysis(result, payload) == result


@pytest.mark.parametrize("citation", [
    {"source_id": "log:nonexistent", "quote": "Unknown column 'billing_mode'"},
    {"source_id": "log:group-sql", "quote": "Connection refused"},
    {"source_id": "log:group-sql", "quote": "Unknown column field list"},
    {"source_id": "log:group-sql", "quote": "Read timed out contacting downstream"},
])
def test_source_and_contiguous_quote_must_exist_together(payload, citation):
    result = model_result()
    result["findings"][0]["evidence"] = [citation]
    with pytest.raises(ValueError, match="Unverifiable evidence"):
        analysis.validate_analysis(result, payload)


@pytest.mark.parametrize("citation", [
    {"source_id": "cross_checks", "quote": "5xx samples unavailable"},
    {"source_id": "log:group-timeout", "quote": "Read timed out contacting downstream"},
])
def test_a_finding_needs_its_own_log_not_only_metrics_or_other_log(payload, citation):
    result = model_result()
    result["findings"][0]["evidence"] = [citation]
    with pytest.raises(ValueError, match="lacks its own log evidence"):
        analysis.validate_analysis(result, payload)


def test_unknown_group_is_rejected_even_with_valid_other_evidence(payload):
    result = model_result()
    result["findings"][0]["group_ids"].append("unknown-group")
    with pytest.raises(ValueError, match="Unknown log group"):
        analysis.validate_analysis(result, payload)


def test_omitted_group_is_rejected(payload):
    result = model_result()
    result["findings"].pop()
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        analysis.validate_analysis(result, payload)


@pytest.mark.parametrize("within_one_finding", [True, False])
def test_duplicate_group_is_rejected(payload, within_one_finding):
    result = model_result()
    if within_one_finding:
        result["findings"][0]["group_ids"].append("group-sql")
    else:
        result["findings"].append(deepcopy(result["findings"][0]))
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        analysis.validate_analysis(result, payload)


def test_related_groups_can_merge_with_own_log_evidence_and_metrics(payload):
    result = model_result()
    merged = result["findings"][0]
    merged["group_ids"] = ["group-sql", "group-timeout"]
    merged["evidence"].append({"source_id": "cross_checks", "quote": "5xx samples unavailable"})
    result["findings"] = [merged]
    validated = analysis.validate_analysis(result, payload)
    assert len(validated["findings"]) == 1


@pytest.mark.parametrize("quote", ["group-sql", "pay-service"])
def test_group_metadata_alone_does_not_establish_log_evidence(payload, quote):
    result = model_result()
    result["findings"][0]["evidence"][0]["quote"] = quote
    with pytest.raises(ValueError):
        analysis.validate_analysis(result, payload)


def test_redaction_preserves_nested_data_types_and_never_mutates_source():
    group = {"id": "safe-group", "service": "pay-service", "count": 4,
             "examples": ["password=FAKE_SECRET phone=13812345678 email=person@example.test", "order=123456789012345678"],
             "metadata": {"enabled": True, "none": None, "latency": 1.5}}
    cross_checks = {"services": {"pay-service": {"metrics": [{"value": 2, "detail": "token=FAKE_TOKEN"}]}}}
    original = deepcopy((group, cross_checks))
    result = analysis.source_payload([group], cross_checks, scope={"count": 4, "complete": False})
    serialized = json.dumps(result, ensure_ascii=False)
    assert json.loads(serialized) == result
    for sensitive in ("FAKE_SECRET", "13812345678", "person@example.test", "123456789012345678", "FAKE_TOKEN"):
        assert sensitive not in serialized
    assert result["groups"][0]["count"] == 4
    assert result["groups"][0]["metadata"] == {"enabled": True, "none": None, "latency": 1.5}
    assert result["scope"] == {"count": 4, "complete": False}
    assert result["sources"]["log:safe-group"] == result["groups"][0]
    assert (group, cross_checks) == original


def test_hash_identity_survives_phone_like_digits_but_log_contents_still_redact():
    # This is a stable generated hash, not a telephone number. Its middle digits
    # happen to match the phone pattern, as occurred in a real collected group.
    identifier = "c15092365925a556b100"
    message = "Client failed with phone-like value 15092365925"
    group = {"id": identifier, "service": "account-service", "count": 2290,
             "summary": message, "examples": [message], "logger": "AccountClient"}
    original = deepcopy(group)
    payload = analysis.source_payload([group], {}, scope={"total_errors": 2290})
    assert payload["groups"][0]["id"] == identifier
    assert set(payload["sources"]) == {f"log:{identifier}", "cross_checks"}
    source = payload["sources"][f"log:{identifier}"]
    assert source["id"] == identifier
    assert "15092365925" not in source["summary"]
    assert "15092365925" not in source["examples"][0]
    assert "[PHONE]" in source["summary"]
    value = model_result()
    value["findings"] = [finding(identifier, "Client failed with phone-like value")]
    assert analysis.validate_analysis(value, payload)["findings"][0]["group_ids"] == [identifier]
    assert group == original


@pytest.mark.parametrize("value", [
    "Authorization: Bearer FAKE_SECRET",
    '"authorization":"Bearer FAKE_SECRET"',
    "'Authorization' : 'Bearer FAKE_SECRET'",
    '"access_token":"FAKE_SECRET"',
    '"refresh_token":"FAKE_SECRET"',
])
def test_header_and_json_credential_values_are_redacted(value):
    cleaned = analysis.redact_text(value)
    assert "FAKE_SECRET" not in cleaned


def test_embedded_json_redaction_remains_valid_json():
    value = json.dumps({"authorization": "Bearer FAKE_SECRET", "password": "OTHER_SECRET", "count": 3})
    cleaned = analysis.redact_text(value)
    decoded = json.loads(cleaned)
    assert decoded["count"] == 3
    assert "FAKE_SECRET" not in cleaned
    assert "OTHER_SECRET" not in cleaned


def test_model_output_requires_exact_config_and_no_extra_fields(payload):
    for field, value in [("model", "other-model"), ("reasoning_effort", "low"), ("hidden_action", "execute")]:
        result = model_result()
        result[field] = value
        with pytest.raises(ValueError):
            analysis.validate_analysis(result, payload)


def test_empty_or_oversized_action_is_rejected(payload):
    for action in (" ", "x" * 201):
        result = model_result()
        result["findings"][0]["actions"] = [action]
        with pytest.raises(ValueError, match="Invalid action"):
            analysis.validate_analysis(result, payload)
