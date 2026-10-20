"""Strict, tool-free approval assessment contract shared with the local model host."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.errors import AppError

MODEL = "gpt-6-sol"
REASONING_EFFORT = "xhigh"
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_RESULT_BYTES = 64 * 1024
USAGE_KEYS = {
    "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"
}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["task", "detail", "human_feedback"]
    quote: str = Field(min_length=1, max_length=2000)


class BusinessDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["approve", "needs_human"]
    risk: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1, max_length=2000)
    reasons: list[str] = Field(min_length=1, max_length=10)
    questions: list[str] = Field(max_length=5)
    evidence: list[Evidence] = Field(max_length=20)
    missing_evidence: list[str] = Field(max_length=10)

    @model_validator(mode="after")
    def safe_decision(self):
        for text in [*self.reasons, *self.questions, *self.missing_evidence]:
            if not text.strip() or len(text) > 2000:
                raise ValueError("Invalid assessment text")
        if not self.summary.strip():
            raise ValueError("Missing assessment summary")
        if self.decision == "approve" and (
            self.risk != "low" or self.questions or self.missing_evidence or not self.evidence
        ):
            raise ValueError("Automatic approval requires complete low-risk evidence")
        return self


class ModelDecision(BusinessDecision):
    model: Literal["gpt-6-sol"]
    reasoning_effort: Literal["xhigh"]
    usage: dict[str, int] | None = None

    @model_validator(mode="after")
    def valid_usage(self):
        if self.usage is not None and (
            not self.usage.keys() <= USAGE_KEYS or any(value < 0 for value in self.usage.values())
        ):
            raise ValueError("Invalid token usage")
        return self


DECISION_SCHEMA = BusinessDecision.model_json_schema()


def _invalid_response() -> AppError:
    return AppError("approval_model_invalid_response", "模型返回的审批判断未通过校验，保留待审批。", 502)


def read_token(path: str | Path) -> str:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            token = stream.read(129).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("Invalid model token")
    except (OSError, UnicodeError, ValueError):
        raise AppError(
            "approval_model_configuration", "模型服务未配置，请检查本机模型服务及令牌文件。", 503
        ) from None
    return token


def encode_payload(payload: dict[str, Any]) -> bytes:
    try:
        if not isinstance(payload, dict):
            raise ValueError("Assessment payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("Assessment input exceeds limit")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AppError("approval_model_input", "审批材料过大或格式异常，需要人工检查。", 422) from None
    return encoded


def validate_evidence(decision: BusinessDecision, payload: dict[str, Any]) -> None:
    for evidence in decision.evidence:
        if not evidence.quote.strip():
            raise ValueError("Evidence must contain an actual quotation")
        source = evidence.source
        if source == "human_feedback":
            value = payload.get("human_feedback", [])
        else:
            approval = payload.get("untrusted_approval", {})
            value = approval.get(source) if isinstance(approval, dict) else None
        if value is None:
            raise ValueError("Unknown evidence source")
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if evidence.quote not in text:
            raise ValueError("Evidence quote is absent from the supplied material")


def validate_model_decision(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = ModelDecision.model_validate(value)
        validate_evidence(result, payload)
        return result.model_dump(exclude_none=True)
    except (ValidationError, ValueError, TypeError, OverflowError, RecursionError):
        raise _invalid_response() from None


class ApprovalModelClient:
    """One bounded HTTP call; inference errors never become an approval."""

    def __init__(self, settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(
            timeout=settings.approval_model_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.approval_model_enabled:
            raise AppError("approval_model_disabled", "后台模型判断已暂停。", 503)
        if (
            self.settings.approval_model != MODEL
            or self.settings.approval_model_reasoning_effort != REASONING_EFFORT
        ):
            raise AppError("approval_model_configuration", "模型配置与授权的型号和思考强度不符。", 503)
        origin = self.settings.approval_model_url.rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
        ):
            raise AppError("approval_model_configuration", "模型服务必须使用本机受保护的地址。", 503)
        token = read_token(self.settings.approval_model_token_file)
        encoded = encode_payload(payload)
        try:
            with self.http.stream(
                "POST", f"{origin}/assess",
                content=encoded,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    error_body = bytearray()
                    for chunk in response.iter_bytes():
                        error_body.extend(chunk)
                        if len(error_body) > 4096:
                            break
                    provider_code = ""
                    if len(error_body) <= 4096:
                        try:
                            error_data = json.loads(error_body)
                            if isinstance(error_data, dict):
                                provider_code = error_data.get("code", "")
                        except (ValueError, UnicodeError, RecursionError):
                            pass
                    known_errors = {
                        "approval_model_configuration": "本机模型隔离配置未通过校验，保留待审批。",
                        "approval_model_tool_attempt": "模型尝试调用未授权工具，本次判断已停止，保留待审批。",
                        "approval_model_invalid_response": "模型返回的审批判断未通过校验，保留待审批。",
                    }
                    if isinstance(provider_code, str) and provider_code in known_errors:
                        raise AppError(provider_code, known_errors[provider_code], 503)
                if response.status_code == 429:
                    raise AppError("approval_model_busy", "模型服务正在处理其他审批，稍后重试。", 503)
                if response.status_code == 504:
                    raise AppError("approval_model_timeout", "模型判断超时，保留待审批并稍后重试。", 503)
                if response.status_code != 200:
                    raise AppError("approval_model_unavailable", "模型服务暂不可用，保留待审批。", 503)
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESULT_BYTES:
                        raise _invalid_response()
                data = json.loads(chunks)
        except httpx.TimeoutException:
            raise AppError(
                "approval_model_timeout", "模型判断超时，保留待审批并稍后重试。", 503
            ) from None
        except httpx.HTTPError:
            raise AppError(
                "approval_model_unavailable", "暂时无法连接本机模型服务，保留待审批。", 503
            ) from None
        except (ValueError, UnicodeError, RecursionError):
            raise _invalid_response() from None
        return validate_model_decision(data, payload)

    def health(self) -> dict[str, Any]:
        try:
            with self.http.stream(
                "GET", self.settings.approval_model_url.rstrip("/") + "/healthz", timeout=3
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Unavailable model host")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 4096:
                        raise ValueError("Invalid model host health")
                data = json.loads(content)
            if (
                not isinstance(data, dict) or data.get("ready") is not True
                or data.get("runtime_configured") is not True
                or data.get("model") != MODEL or data.get("reasoning_effort") != REASONING_EFFORT
            ):
                raise ValueError("Model runtime is not ready")
            return {
                "ready": True, "runtime_configured": True,
                "model": MODEL, "reasoning_effort": REASONING_EFFORT,
                "readiness_scope": "local_runtime",
                "last_assessment_succeeded": data.get("last_assessment_succeeded") is True,
            }
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise AppError(
                "approval_model_unavailable", "本机模型运行环境尚未就绪，保留待审批。", 503
            ) from None

    def close(self) -> None:
        self.http.close()
