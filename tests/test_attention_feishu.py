import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.errors import AppError, FeishuError
from app.feishu import FeishuClient
from app.models import Account

BOT_SCOPES = "im:message:send_as_bot im:message.urgent:phone"


def bot_token(scope=BOT_SCOPES):
    return {
        "code": 0, "access_token": "bot-access", "expires_in": 7200,
        "token_type": "Bearer", "scope": scope,
    }


def make_client(settings, handler, *, owner="ou_owner", user_scope=""):
    @contextmanager
    def sessions():
        def get(model, identifier):
            assert model is Account and identifier == 1
            return SimpleNamespace(open_id=owner, scope=user_scope) if owner else None

        yield SimpleNamespace(get=get)

    client = FeishuClient(
        settings, None, sessions, transport=httpx.MockTransport(handler),
        sleep=lambda _: pytest.fail("Attention writes must not wait and retry"),
    )
    client.access_token = lambda: "user-access"
    return client


def test_bot_permissions_come_from_client_credentials_response_not_user_scope(settings):
    calls = []
    returned = {"scope": ""}

    def handler(request):
        calls.append(request)
        assert request.url.path == "/oauth/v3/token"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["grant_type"] == "client_credentials"
        assert "refresh_token" not in body
        assert body["client_id"] == settings.feishu_app_id
        return httpx.Response(200, json=bot_token(returned["scope"]))

    client = make_client(settings, handler, user_scope=BOT_SCOPES)
    assert client.attention_permissions() == {
        "ready": False,
        "missing_scopes": ["im:message:send_as_bot", "im:message.urgent:phone"],
    }
    with pytest.raises(AppError) as caught:
        client.send_self_reminder("重要消息待处理", "stable-id")
    assert caught.value.code == "attention_permission_required"
    assert len(calls) == 1
    returned["scope"] = BOT_SCOPES
    client._attention_token_until = 0
    assert client.attention_permissions() == {"ready": True, "missing_scopes": []}
    assert len(calls) == 2
    client.close()


@pytest.mark.parametrize("scope", [None, 123, ["im:message.urgent:phone"]])
def test_malformed_bot_scope_never_grants_phone_access(settings, scope):
    client = make_client(
        settings, lambda _: httpx.Response(200, json=bot_token(scope)), user_scope=BOT_SCOPES,
    )
    with pytest.raises(FeishuError) as caught:
        client.attention_permissions()
    assert caught.value.code == "feishu_invalid_token_response"
    client.close()


def test_reminder_and_phone_target_only_bound_owner_with_bot_token(settings):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/oauth/v3/token":
            return httpx.Response(200, json=bot_token())
        assert request.headers["authorization"] == "Bearer bot-access"
        if request.url.path == "/open-apis/im/v1/messages":
            assert request.method == "POST"
            assert request.url.params["receive_id_type"] == "open_id"
            assert json.loads(request.content) == {
                "receive_id": "ou_owner", "msg_type": "text",
                "content": json.dumps({"text": "重要消息待处理"}, ensure_ascii=False),
                "uuid": "one-stable-reminder",
            }
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_reminder"}})
        assert request.method == "PATCH"
        assert request.url.path == "/open-apis/im/v1/messages/om_reminder/urgent_phone"
        assert request.url.params["user_id_type"] == "open_id"
        assert json.loads(request.content) == {"user_id_list": ["ou_owner"]}
        return httpx.Response(200, json={"code": 0, "data": {"invalid_user_id_list": []}})

    client = make_client(settings, handler, user_scope="")
    assert client.send_self_reminder("重要消息待处理", "one-stable-reminder") == {
        "message_id": "om_reminder"
    }
    assert client.phone_self("om_reminder") == {"invalid_user_id_list": []}
    assert [call.method for call in calls] == ["POST", "POST", "PATCH"]
    client.close()


@pytest.mark.parametrize("operation", ["send", "phone"])
def test_missing_bound_owner_cannot_send_or_call(settings, operation):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/oauth/v3/token"
        return httpx.Response(200, json=bot_token())

    client = make_client(settings, handler, owner=None)
    with pytest.raises(AppError) as caught:
        if operation == "send":
            client.send_self_reminder("重要消息待处理", "stable-id")
        else:
            client.phone_self("om_reminder")
    assert caught.value.code == "connect_required"
    assert len(calls) == 1
    client.close()


@pytest.mark.parametrize("failure", ["timeout", "token_expired", "rate_limit", "server_error"])
def test_phone_request_is_sent_once_even_when_the_response_is_ambiguous(settings, failure):
    writes = []

    def handler(request):
        if request.url.path == "/oauth/v3/token":
            return httpx.Response(200, json=bot_token())
        writes.append(request)
        assert request.method == "PATCH"
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic response lost", request=request)
        if failure == "token_expired":
            return httpx.Response(200, json={"code": 99991677})
        if failure == "rate_limit":
            return httpx.Response(429, json={"code": 99991400})
        return httpx.Response(503, json={"code": 99999999})

    client = make_client(settings, handler)
    with pytest.raises(FeishuError):
        client.phone_self("om_reminder")
    assert len(writes) == 1
    client.close()


@pytest.mark.parametrize("data", [
    {}, None, {"invalid_user_id_list": None}, {"invalid_user_id_list": "[]"},
    {"invalid_user_id_list": ["ou_owner"]}, {"invalid_user_id_list": ["ou_other"]},
])
def test_success_code_without_confirmed_recipient_is_not_a_successful_phone_request(
    settings, data,
):
    client = make_client(settings, lambda request: httpx.Response(
        200, json=bot_token() if request.url.path == "/oauth/v3/token" else {"code": 0, "data": data}
    ))
    with pytest.raises(FeishuError) as caught:
        client.phone_self("om_reminder")
    assert caught.value.code == "feishu_phone_recipient_unconfirmed"
    client.close()


def test_read_status_distinguishes_explicit_unread_from_unknown(settings):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/open-apis/im/v1/messages/read_status"
        assert request.headers["authorization"] == "Bearer user-access"
        return httpx.Response(200, json={"code": 0, "data": {
            "items": [
                {"message_id": "om_unread", "is_read": False},
                {"message_id": "om_read", "is_read": True},
                {"message_id": "om_invalid", "is_read": False},
                {"message_id": "om_string", "is_read": "false"},
                {"message_id": "om_number", "is_read": 0},
                {"message_id": "om_no_status"},
            ],
            "invalid_message_ids": ["om_invalid"],
        }})

    client = make_client(settings, handler)
    identifiers = [
        "om_unread", "om_read", "om_invalid", "om_string", "om_number", "om_no_status", "om_absent"
    ]
    assert client.message_read_status(identifiers) == {
        "om_unread": False, "om_read": True,
        **dict.fromkeys(identifiers[2:]),
    }
    assert json.loads(calls[0].content) == {"message_ids": identifiers}
    client.close()


@pytest.mark.parametrize("data", [
    None, {}, {"items": None}, {"items": [] , "invalid_message_ids": "invalid"},
    {"items": ["invalid"]},
    {"items": [
        {"message_id": "om_one", "is_read": True},
        {"message_id": "om_one", "is_read": False},
    ]},
])
def test_unusable_read_status_response_fails_closed(settings, data):
    client = make_client(
        settings, lambda _: httpx.Response(200, json={"code": 0, "data": data})
    )
    with pytest.raises(FeishuError) as caught:
        client.message_read_status(["om_one"])
    assert caught.value.code == "feishu_invalid_read_status"
    client.close()


@pytest.mark.parametrize("identifiers", [[], ["om_one", "om_one"], [f"om_{i}" for i in range(51)]])
def test_read_status_rejects_unbounded_or_duplicate_requests_before_network(settings, identifiers):
    client = make_client(settings, lambda _: pytest.fail("Invalid input must not make a request"))
    with pytest.raises(AppError) as caught:
        client.message_read_status(identifiers)
    assert caught.value.code == "invalid_message_ids"
    client.close()


def test_message_fetch_returns_only_the_requested_message(settings):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/open-apis/im/v1/messages/om_source"
        assert request.url.params["user_id_type"] == "open_id"
        assert request.headers["authorization"] == "Bearer user-access"
        return httpx.Response(200, json={"code": 0, "data": {"items": [
            {"message_id": "om_other", "body": {"content": "other"}},
            {"message_id": "om_source", "body": {"content": "source"}},
        ]}})

    client = make_client(settings, handler)
    assert client.message("om_source") == {
        "message_id": "om_source", "body": {"content": "source"}
    }
    client.close()


@pytest.mark.parametrize("items", [
    None, [], [{"message_id": "om_other"}],
    [{"message_id": "om_source"}, {"message_id": "om_source"}],
])
def test_missing_or_ambiguous_source_message_is_never_used(settings, items):
    client = make_client(settings, lambda _: httpx.Response(
        200, json={"code": 0, "data": {"items": items}}
    ))
    with pytest.raises(FeishuError) as caught:
        client.message("om_source")
    assert caught.value.code == "feishu_invalid_message_response"
    client.close()
