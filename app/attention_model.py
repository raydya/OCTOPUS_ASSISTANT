"""Bounded, evidence-checked message importance assessment through the local model host."""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.approval_model import (
    MAX_PAYLOAD_BYTES,
    MAX_RESULT_BYTES,
    MODEL,
    REASONING_EFFORT,
    USAGE_KEYS,
    read_token,
)
from app.errors import AppError


class MessageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["message", "context"]
    quote: str = Field(min_length=1, max_length=2000)


class MessageImportance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    importance: Literal["high", "normal"]
    requires_response: bool
    summary: str = Field(min_length=1, max_length=2000)
    reasons: list[str] = Field(min_length=1, max_length=10)
    evidence: list[MessageEvidence] = Field(max_length=20)

    @model_validator(mode="after")
    def valid_assessment(self):
        if not self.summary.strip() or any(
            not text.strip() or len(text) > 2000 for text in self.reasons
        ):
            raise ValueError("Invalid assessment text")
        if self.importance == "high" and self.requires_response and not any(
            evidence.source == "message" and len(evidence.quote.strip()) >= 6
            for evidence in self.evidence
        ):
            raise ValueError("Escalation requires specific evidence in the message itself")
        return self


class MessageModelDecision(MessageImportance):
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


MESSAGE_DECISION_SCHEMA = MessageImportance.model_json_schema()


def invalid_message_response() -> AppError:
    return AppError(
        "attention_model_invalid_response", "消息重要性判断未通过校验，本次不触发电话提醒。", 502
    )


def encode_message_payload(payload: dict[str, Any]) -> bytes:
    try:
        if (
            not isinstance(payload, dict) or set(payload) != {"message", "context"}
            or not isinstance(payload["message"], dict) or not payload["message"]
            or not isinstance(payload["context"], list) or len(payload["context"]) > 100
            or any(not isinstance(item, dict) for item in payload["context"])
        ):
            raise ValueError("Expected only a message and bounded context")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("Message input exceeds limit")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AppError(
            "attention_model_input", "消息材料过大或格式异常，本次不触发电话提醒。", 422
        ) from None


def _text_values(value: Any):
    """Quote actual string values, never JSON field names or serialized metadata syntax."""
    if isinstance(value, str):
        yield value
        # Feishu body.content is often a JSON-encoded string.
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                return
            if isinstance(decoded, (dict, list)):
                yield from _text_values(decoded)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _text_values(child)


def validate_message_decision(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        encode_message_payload(payload)
        result = MessageModelDecision.model_validate(value)
        for evidence in result.evidence:
            if not evidence.quote.strip() or not any(
                evidence.quote in text for text in _text_values(payload[evidence.source])
            ):
                raise ValueError("Evidence is absent from the specified source")
        return result.model_dump(exclude_none=True)
    except (ValidationError, ValueError, TypeError, OverflowError, RecursionError):
        raise invalid_message_response() from None


def message_model_error(code: str, status: int = 503) -> AppError:
    suffix = code.removeprefix("approval_model_").removeprefix("attention_model_")
    messages = {
        "configuration": "本机消息模型配置未通过校验，本次不触发电话提醒。",
        "tool_attempt": "模型尝试调用未授权工具，消息判断已停止。",
        "invalid_response": "消息重要性判断未通过校验，本次不触发电话提醒。",
        "busy": "模型服务正忙，消息判断稍后重试。",
        "timeout": "消息重要性判断超时，本次不触发电话提醒。",
        "unavailable": "消息模型服务暂不可用，本次不触发电话提醒。",
    }
    if suffix not in messages:
        suffix = "unavailable"
    return AppError(f"attention_model_{suffix}", messages[suffix], status)


class MessageImportanceClient:
    """One local HTTP assessment; the service that calls this owns scheduling and retry."""

    def __init__(self, settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(
            timeout=settings.approval_model_timeout_seconds,
            follow_redirects=False, trust_env=False, transport=transport,
        )

    def assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (
            self.settings.approval_model != MODEL
            or self.settings.approval_model_reasoning_effort != REASONING_EFFORT
        ):
            raise message_model_error("configuration")
        origin = self.settings.approval_model_url.rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
        ):
            raise message_model_error("configuration")
        try:
            token = read_token(self.settings.approval_model_token_file)
        except AppError:
            raise message_model_error("configuration") from None
        encoded = encode_message_payload(payload)
        try:
            with self.http.stream(
                "POST", f"{origin}/assess-message", content=encoded,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            ) as response:
                chunks = bytearray()
                limit = MAX_RESULT_BYTES if response.status_code == 200 else 4096
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        if response.status_code == 200:
                            raise invalid_message_response()
                        raise message_model_error("unavailable")
                if response.status_code != 200:
                    if response.status_code == 429:
                        raise message_model_error("busy")
                    if response.status_code == 504:
                        raise message_model_error("timeout")
                    try:
                        data = json.loads(chunks)
                        code = data.get("code") if isinstance(data, dict) else None
                    except (ValueError, UnicodeError, RecursionError):
                        code = None
                    raise message_model_error(code if isinstance(code, str) else "unavailable")
                data = json.loads(chunks)
        except httpx.TimeoutException:
            raise message_model_error("timeout") from None
        except httpx.HTTPError:
            raise message_model_error("unavailable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise invalid_message_response() from None
        return validate_message_decision(data, payload)

    def close(self) -> None:
        self.http.close()
