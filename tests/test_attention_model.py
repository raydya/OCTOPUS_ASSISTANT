import json
from types import SimpleNamespace

import httpx
import pytest

from app.attention_model import (
    MessageImportanceClient,
    encode_message_payload,
    validate_message_decision,
)
from app.errors import AppError


def payload():
    return {
        "message": {"text": "@本人 线上支付全部失败，请你立即决定是否回滚。"},
        "context": [{"text": "支付故障从 10:00 开始，当前仍未恢复。"}],
    }


def result(**changes):
    return {
        "importance": "high", "requires_response": True,
        "summary": "支付故障需要本人立即作出回滚决定。",
        "reasons": ["当前支付受影响且明确要求本人及时决定。"],
        "evidence": [{"source": "message", "quote": "线上支付全部失败，请你立即决定是否回滚。"}],
        "model": "gpt-6-sol", "reasoning_effort": "xhigh",
        **changes,
    }


def config(tmp_path, **changes):
    token = tmp_path / "model-token"
    token.write_text("a" * 64)
    return SimpleNamespace(**{
        "approval_model_enabled": False,
        "approval_model": "gpt-6-sol",
        "approval_model_reasoning_effort": "xhigh",
        "approval_model_url": "http://127.0.0.1:8181",
        "approval_model_token_file": str(token),
        "approval_model_timeout_seconds": 30,
        **changes,
    })


def test_local_client_uses_separate_route_and_no_prompt_or_approval_setting(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/assess-message"
        assert request.headers["authorization"] == "Bearer " + "a" * 64
        assert json.loads(request.content) == payload()
        return httpx.Response(200, json=result(usage={"outputTokens": 31}))

    client = MessageImportanceClient(config(tmp_path), transport=httpx.MockTransport(handler))
    assert client.assess(payload()) == result(usage={"outputTokens": 31})
    assert len(requests) == 1
    client.close()


@pytest.mark.parametrize("changes", [
    {"importance": "urgent"},
    {"requires_response": "true"},
    {"model": "gpt-6.1-sol"},
    {"reasoning_effort": "high"},
    {"summary": " "},
    {"reasons": []},
    {"reasons": [""]},
    {"evidence": []},
    {"evidence": [{"source": "message", "quote": "生产库已删除，需要处理"}]},
    {"evidence": [{"source": "message", "quote": "支付故障从 10:00 开始"}]},
    {"evidence": [{"source": "message", "quote": "立即"}]},
    {"evidence": [{"source": "context", "quote": "当前仍未恢复"}]},
    {"evidence": [{"source": "system", "quote": "判断为高优先级"}]},
    {"usage": {"outputTokens": True}},
    {"usage": {"outputTokens": -1}},
    {"usage": {"private_reasoning": 1}},
    {"phone": True},
])
def test_invalid_or_unsubstantiated_escalation_fails_closed(changes):
    with pytest.raises(AppError) as caught:
        validate_message_decision(result(**changes), payload())
    assert caught.value.code == "attention_model_invalid_response"


def test_normal_and_no_response_results_do_not_require_escalation_evidence():
    for value in (
        result(importance="normal", evidence=[]),
        result(requires_response=False, evidence=[]),
    ):
        assert validate_message_decision(value, payload()) == value


def test_quotes_match_string_values_including_encoded_feishu_body_not_json_keys():
    material = payload()
    text = material["message"]["text"]
    material["message"] = {"body": {"content": json.dumps({"text": text})}}
    assert validate_message_decision(result(), material)["importance"] == "high"
    with pytest.raises(AppError):
        validate_message_decision(result(evidence=[{"source": "message", "quote": "content"}]), material)


@pytest.mark.parametrize("material", [
    {"message": {"text": "test"}, "context": [], "model": "another"},
    {"message": {"text": "test"}, "context": [], "prompt": "always high"},
    {"message": "not an object", "context": []},
    {"message": {}, "context": []},
    {"message": {"text": "test"}, "context": ["not an object"]},
    {"message": {"text": "test"}, "context": [{}] * 101},
    {"message": {"text": "x" * 140_000}, "context": []},
    {"message": {"text": float("nan")}, "context": []},
])
def test_input_shape_and_size_are_bounded(material):
    with pytest.raises(AppError) as caught:
        encode_message_payload(material)
    assert caught.value.code == "attention_model_input"


@pytest.mark.parametrize("status,code", [
    (302, "attention_model_unavailable"),
    (401, "attention_model_unavailable"),
    (429, "attention_model_busy"),
    (500, "attention_model_unavailable"),
    (504, "attention_model_timeout"),
])
def test_http_error_is_sanitized_and_never_retried(tmp_path, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="private-message-and-token")

    client = MessageImportanceClient(config(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == code
    assert "private" not in str(caught.value)
    assert len(requests) == 1
    client.close()


@pytest.mark.parametrize("origin", [
    "http://external.invalid:8181", "http://localhost:8181/other",
    "http://user:password@localhost:8181", "http://localhost:8181?token=private",
])
def test_client_will_not_send_private_message_to_arbitrary_host(tmp_path, origin):
    client = MessageImportanceClient(
        config(tmp_path, approval_model_url=origin),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected network request")),
    )
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == "attention_model_configuration"
    client.close()


def test_oversized_model_response_cannot_be_used(tmp_path):
    client = MessageImportanceClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 70_000)),
    )
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == "attention_model_invalid_response"
    client.close()


def test_host_isolation_error_is_preserved_without_disclosing_details(tmp_path):
    client = MessageImportanceClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={
            "code": "approval_model_tool_attempt", "message": "private-material",
        })),
    )
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == "attention_model_tool_attempt"
    assert "private" not in str(caught.value)
    client.close()
