import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.errors import AppError, FeishuError
from app.feishu import FeishuClient
from app.models import Account


def bot_token(scope="im:message:send_as_bot"):
    return {
        "code": 0, "access_token": "status-bot-access", "expires_in": 7200,
        "token_type": "Bearer", "scope": scope,
    }


@pytest.fixture
def status_client(settings):
    clients = []

    def make(handler, *, owner="ou_owner", user_scope=""):
        identity = {"owner": owner}

        @contextmanager
        def sessions():
            def get(model, identifier):
                assert model is Account and identifier == 1
                if identity["owner"] is None:
                    return None
                return SimpleNamespace(open_id=identity["owner"], scope=user_scope)

            yield SimpleNamespace(get=get)

        client = FeishuClient(
            settings, None, sessions, transport=httpx.MockTransport(handler),
            sleep=lambda _: pytest.fail("Status sends must never retry"),
        )
        client.access_token = lambda: pytest.fail("Status sends must not use a user token")
        clients.append(client)
        return client, identity

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize("scope", ["im:message:send_as_bot", "im:message", "im:message:send"])
def test_status_sends_with_only_bot_message_permission_to_bound_owner(status_client, scope):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/oauth/v3/token":
            assert json.loads(request.content)["grant_type"] == "client_credentials"
            return httpx.Response(200, json=bot_token(scope))
        assert request.method == "POST"
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.url.params["receive_id_type"] == "open_id"
        assert request.headers["authorization"] == "Bearer status-bot-access"
        assert json.loads(request.content) == {
            "receive_id": "ou_owner", "msg_type": "text",
            "content": json.dumps({"text": "审批状态：需要你介入。"}, ensure_ascii=False),
            "uuid": "stable-status-id",
        }
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_status"}})

    client, _ = status_client(handler)
    assert client.self_message_permissions() == {"ready": True, "missing_scopes": []}
    assert client.attention_permissions() == {
        "ready": False, "missing_scopes": ["im:message.urgent:phone"],
    }
    assert client.send_self_status(
        "审批状态：需要你介入。", "stable-status-id", expected_owner="ou_owner"
    ) == {"message_id": "om_status"}
    assert len(calls) == 2


def test_status_permission_uses_app_grants_even_if_user_has_send_scope(status_client):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/oauth/v3/token"
        return httpx.Response(200, json=bot_token("im:message.urgent:phone"))

    client, _ = status_client(handler, user_scope="im:message:send_as_bot")
    assert client.self_message_permissions() == {
        "ready": False, "missing_scopes": ["im:message:send_as_bot"],
    }
    with pytest.raises(AppError) as error:
        client.send_self_status("待你处理", "status-id", expected_owner="ou_owner")
    assert error.value.code == "self_message_permission_required"
    assert len(calls) == 1


@pytest.mark.parametrize("owner,code", [(None, "connect_required"), ("ou_other", "status_owner_changed")])
def test_missing_or_changed_binding_prevents_status_send(status_client, owner, code):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/oauth/v3/token"
        return httpx.Response(200, json=bot_token())

    client, _ = status_client(handler, owner=owner)
    with pytest.raises(AppError) as error:
        client.send_self_status("待你处理", "status-id", expected_owner="ou_owner")
    assert error.value.code == code
    assert len(calls) == 1


def test_binding_is_checked_again_after_token_request(status_client):
    def handler(request):
        assert request.url.path == "/oauth/v3/token"
        identity["owner"] = "ou_replaced_during_refresh"
        return httpx.Response(200, json=bot_token())

    client, identity = status_client(handler)
    with pytest.raises(AppError) as error:
        client.send_self_status("待你处理", "status-id", expected_owner="ou_owner")
    assert error.value.code == "status_owner_changed"


@pytest.mark.parametrize("failure", ["timeout", "token_expired", "rate_limit", "server_error"])
def test_status_post_is_attempted_once_on_failure(status_client, failure):
    sends = []

    def handler(request):
        if request.url.path == "/oauth/v3/token":
            return httpx.Response(200, json=bot_token())
        sends.append(request)
        assert request.method == "POST"
        assert request.url.path == "/open-apis/im/v1/messages"
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic response lost", request=request)
        if failure == "token_expired":
            return httpx.Response(200, json={"code": 99991677})
        if failure == "rate_limit":
            return httpx.Response(429, json={"code": 99991400})
        return httpx.Response(503, json={"code": 99999999})

    client, _ = status_client(handler)
    with pytest.raises(FeishuError):
        client.send_self_status("待你处理", "status-id", expected_owner="ou_owner")
    assert len(sends) == 1


@pytest.mark.parametrize("message,uuid,owner,code", [
    (None, "status-id", "ou_owner", "invalid_status_message"),
    ("", "status-id", "ou_owner", "invalid_status_message"),
    (" \n", "status-id", "ou_owner", "invalid_status_message"),
    ("字" * 10001, "status-id", "ou_owner", "invalid_status_message"),
    ("状态", None, "ou_owner", "invalid_status_message"),
    ("状态", "", "ou_owner", "invalid_status_message"),
    ("状态", "x" * 51, "ou_owner", "invalid_status_message"),
    ("状态", "invalid id", "ou_owner", "invalid_status_message"),
    ("状态", "id\n", "ou_owner", "invalid_status_message"),
    ("状态", "status-id", None, "status_owner_changed"),
    ("状态", "status-id", "", "status_owner_changed"),
], ids=[
    "no-message", "empty-message", "blank-message", "long-message", "no-uuid", "empty-uuid",
    "long-uuid", "spaced-uuid", "newline-uuid", "no-owner", "empty-owner",
])
def test_invalid_status_payload_is_rejected_before_network(status_client, message, uuid, owner, code):
    client, _ = status_client(lambda _: pytest.fail("Invalid status payload must not make a request"))
    with pytest.raises(AppError) as error:
        client.send_self_status(message, uuid, expected_owner=owner)
    assert error.value.code == code


@pytest.mark.parametrize("data", [
    None, {}, {"message_id": None}, {"message_id": 123}, {"message_id": "not-a-message"},
    {"message_id": "om_"}, {"message_id": "om_invalid id"}, {"message_id": "om_test\n"},
])
def test_status_send_requires_a_valid_returned_message_id(status_client, data):
    client, _ = status_client(lambda request: httpx.Response(
        200, json=bot_token() if request.url.path == "/oauth/v3/token" else {"code": 0, "data": data}
    ))
    with pytest.raises(FeishuError) as error:
        client.send_self_status("待你处理", "status-id", expected_owner="ou_owner")
    assert error.value.code == "feishu_invalid_status_response"
