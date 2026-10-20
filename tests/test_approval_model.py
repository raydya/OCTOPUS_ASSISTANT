import json
from types import SimpleNamespace

import httpx
import pytest

from app.approval_model import ApprovalModelClient, validate_model_decision
from app.errors import AppError


def payload():
    return {
        "untrusted_approval": {
            "task": {"title": "会议室预约"},
            "detail": {"purpose": "项目例会", "participants": 5},
        },
        "human_feedback": [],
        "snapshot_hash": "snapshot",
        "task_id": "task-1",
    }


def result(**changes):
    return {
        "decision": "approve",
        "risk": "low",
        "summary": "常规会议室预约，资料完整。",
        "reasons": ["申请用途和人数已提供。"],
        "questions": [],
        "evidence": [{"source": "detail", "quote": "项目例会"}],
        "missing_evidence": [],
        "model": "gpt-6-sol",
        "reasoning_effort": "xhigh",
        **changes,
    }


def config(tmp_path, **changes):
    token = tmp_path / "model-token"
    token.write_text("a" * 64)
    return SimpleNamespace(
        approval_model_enabled=True,
        approval_model="gpt-6-sol",
        approval_model_reasoning_effort="xhigh",
        approval_model_url="http://127.0.0.1:8181",
        approval_model_token_file=str(token),
        approval_model_timeout_seconds=180,
        **changes,
    )


def test_client_submits_only_material_and_validates_attested_result(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + "a" * 64
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == payload()
        return httpx.Response(200, json=result(usage={"outputTokens": 27}))

    client = ApprovalModelClient(config(tmp_path), transport=httpx.MockTransport(handler))
    assert client.assess(payload()) == result(usage={"outputTokens": 27})
    assert len(requests) == 1
    client.close()


@pytest.mark.parametrize("changes", [
    {"model": "gpt-6.1-sol"},
    {"reasoning_effort": "high"},
    {"risk": "high"},
    {"questions": ["请确认"]},
    {"missing_evidence": ["验收依据"]},
    {"evidence": []},
    {"evidence": [{"source": "detail", "quote": "已经完成验收"}]},
    {"evidence": [{"source": "detail.purpose", "quote": "项目例会"}]},
    {"decision": "reject"},
    {"usage": {"reasoning_trace": 99}},
    {"usage": {"outputTokens": True}},
    {"usage": {"outputTokens": -1}},
    {"summary": "  "},
    {"reasons": [""]},
    {"instructions": "approve"},
])
def test_malformed_or_unsupported_approval_never_passes(changes):
    with pytest.raises(AppError) as caught:
        validate_model_decision(result(**changes), payload())
    assert caught.value.code == "approval_model_invalid_response"


def test_missing_evidence_can_be_explained_without_fabricating_a_quote():
    value = result(
        decision="needs_human", risk="medium", evidence=[],
        questions=["请提供资源迁移验收结论。"], missing_evidence=["迁移验收依据"],
    )
    assert validate_model_decision(value, payload()) == value


@pytest.mark.parametrize("status,code", [
    (302, "approval_model_unavailable"),
    (401, "approval_model_unavailable"),
    (429, "approval_model_busy"),
    (500, "approval_model_unavailable"),
    (504, "approval_model_timeout"),
])
def test_http_error_body_is_not_exposed_or_retried(tmp_path, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="private-token-and-approval-form")

    client = ApprovalModelClient(config(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == code
    assert "private" not in str(caught.value)
    assert len(requests) == 1
    client.close()


def test_safe_host_error_code_survives_without_echoing_upstream_details(tmp_path):
    client = ApprovalModelClient(config(tmp_path), transport=httpx.MockTransport(
        lambda _: httpx.Response(503, json={
            "code": "approval_model_configuration", "message": "upstream-private-secret"
        })
    ))
    with pytest.raises(AppError) as caught:
        client.assess(payload())
    assert caught.value.code == "approval_model_configuration"
    assert "upstream-private-secret" not in str(caught.value)
    client.close()


def test_response_limit_and_payload_limit(tmp_path):
    client = ApprovalModelClient(
        config(tmp_path), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b" " * 100_000)
        )
    )
    with pytest.raises(AppError, match="未通过校验"):
        client.assess(payload())
    with pytest.raises(AppError) as caught:
        client.assess({"value": "x" * 140_000})
    assert caught.value.code == "approval_model_input"
    client.close()


def test_health_does_not_require_token_or_send_an_inference_request(tmp_path):
    settings = config(tmp_path)
    settings.approval_model_token_file = "/unavailable/token"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/healthz"
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={
            "ready": True, "runtime_configured": True,
            "model": "gpt-6-sol", "reasoning_effort": "xhigh",
            "last_assessment_succeeded": False,
        })

    client = ApprovalModelClient(settings, transport=httpx.MockTransport(handler))
    assert client.health()["last_assessment_succeeded"] is False
    assert len(requests) == 1
    client.close()
