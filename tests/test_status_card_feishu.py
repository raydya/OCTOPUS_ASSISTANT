"""Bot-only status cards: persist a self-send receipt before any update."""

import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.errors import FeishuError
from app.feishu import FeishuClient, StatusCardPreflightError
from app.models import Account, utcnow


def card():
    return {
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "审批状态"}},
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "需要你处理"}}],
    }


@pytest.fixture
def cards(settings):
    clients = []

    def make(*, scopes="im:message:send_as_bot im:message:readonly"):
        created = str(int(utcnow().timestamp() * 1000))
        receipt = {
            "message_id": "om_status", "chat_id": "oc_self", "msg_type": "interactive",
            "owner_open_id": "ou_owner", "sender_app_id": "cli_test", "create_time": created,
        }
        remote = SimpleNamespace(
            scopes=scopes, calls=[], token_hook=None, get_hook=None, failure=None, patch_data={},
            message={
                "message_id": "om_status", "chat_id": "oc_self", "msg_type": "interactive",
                "create_time": created, "deleted": False,
                "sender": {"id": "cli_test", "id_type": "app_id", "sender_type": "app"},
            },
        )
        identity = SimpleNamespace(
            owner="ou_owner", source=SimpleNamespace(
                card_message_id="om_status", owner_open_id="ou_owner", card_create_state="ready",
                card_receipt=deepcopy(receipt),
            ),
        )

        @contextmanager
        def sessions():
            def get(model, identifier):
                assert model is Account and identifier == 1
                return SimpleNamespace(open_id=identity.owner) if identity.owner else None

            def scalars(statement):
                values = statement.compile().params
                source = identity.source
                if source and (
                    source.card_message_id == values["card_message_id_1"]
                    and source.owner_open_id == values["owner_open_id_1"]
                    and source.card_create_state == values["card_create_state_1"]
                ):
                    return [source]
                return []

            yield SimpleNamespace(get=get, scalars=scalars)

        def handler(request):
            remote.calls.append(request)
            if request.url.path == "/oauth/v3/token":
                assert json.loads(request.content)["grant_type"] == "client_credentials"
                if remote.token_hook:
                    remote.token_hook()
                return httpx.Response(200, json={
                    "code": 0, "access_token": "bot-token", "expires_in": 7200,
                    "scope": remote.scopes,
                })
            assert request.headers["authorization"] == "Bearer bot-token"
            if request.method == "GET":
                if remote.get_hook:
                    remote.get_hook()
                if remote.failure == "get_timeout":
                    raise httpx.ReadTimeout("read unavailable", request=request)
                if remote.failure == "get_deleted":
                    return httpx.Response(400, json={"code": 230110})
                return httpx.Response(200, json={"code": 0, "data": {"items": [deepcopy(remote.message)]}})
            if remote.failure == "write_timeout":
                raise httpx.ReadTimeout("write response lost", request=request)
            if remote.failure == "write_expired_token":
                return httpx.Response(200, json={"code": 99991677})
            if remote.failure == "write_rate_limit":
                return httpx.Response(429, json={"code": 99991400})
            if remote.failure == "write_server_error":
                return httpx.Response(503, json={"code": 99999999})
            data = deepcopy(remote.message) if request.method == "POST" else remote.patch_data
            return httpx.Response(200, json={"code": 0, "data": data})

        client = FeishuClient(settings, None, sessions, transport=httpx.MockTransport(handler), sleep=lambda _: None)
        client.access_token = lambda *args, **kwargs: pytest.fail("Status cards must never use a user token")
        clients.append(client)
        return SimpleNamespace(client=client, identity=identity, remote=remote, receipt=receipt)

    yield make
    for client in clients:
        client.close()


def writes(system):
    return [request for request in system.remote.calls if request.method in {"POST", "PATCH"} and "/im/v1/messages" in request.url.path]


def update(system):
    return system.client.update_self_status_card("om_status", card(), expected_owner="ou_owner", expected_receipt=system.receipt)


@pytest.mark.parametrize("scope", ["im:message:send_as_bot", "im:message", "im:message:send"])
def test_send_card_uses_bound_open_id_bot_token_and_returns_verifiable_receipt(cards, scope):
    system = cards(scopes=scope)
    result = system.client.send_self_status_card(card(), "status-id", expected_owner="ou_owner")
    assert result == {"message_id": "om_status", "receipt": system.receipt}
    request, = writes(system)
    assert request.method == "POST" and request.url.params["receive_id_type"] == "open_id"
    payload = json.loads(request.content)
    assert payload["receive_id"] == "ou_owner" and payload["msg_type"] == "interactive"
    assert payload["uuid"] == "status-id" and json.loads(payload["content"]) == card()


def test_update_requires_persisted_self_receipt_and_verifies_remote_card(cards):
    system = cards()
    assert update(system) == {"message_id": "om_status"}
    assert [request.method for request in system.remote.calls] == ["POST", "GET", "PATCH"]
    lookup = system.remote.calls[1]
    assert lookup.url.path.endswith("/messages/om_status") and lookup.url.params["user_id_type"] == "open_id"
    patch, = writes(system)
    assert patch.method == "PATCH" and patch.url.path.endswith("/messages/om_status")
    assert json.loads(json.loads(patch.content)["content"]) == card()


@pytest.mark.parametrize("change", ["missing", "owner", "message", "state", "receipt"])
def test_unpersisted_or_wrong_anchor_cannot_update_even_with_plausible_receipt(cards, change):
    system = cards()
    source = system.identity.source
    if change == "missing":
        system.identity.source = None
    elif change == "owner":
        source.owner_open_id = "ou_other"
    elif change == "message":
        source.card_message_id = "om_other"
    elif change == "state":
        source.card_create_state = "unknown"
    else:
        source.card_receipt["chat_id"] = "oc_other"
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_card_unverified" and not caught.value.retryable
    assert not system.remote.calls


@pytest.mark.parametrize("change", ["sender", "sender_type", "chat", "type", "group", "deleted", "created"])
def test_remote_message_mismatch_never_reaches_patch(cards, change):
    system = cards()
    remote = system.remote.message
    if change == "sender":
        remote["sender"]["id"] = "cli_other"
    elif change == "sender_type":
        remote["sender"]["sender_type"] = "user"
    elif change == "chat":
        remote["chat_id"] = "oc_someone_else"
    elif change == "type":
        remote["msg_type"] = "text"
    elif change == "group":
        remote["chat_type"] = "group"
    elif change == "deleted":
        remote["deleted"] = True
    else:
        remote["create_time"] = str(int(remote["create_time"]) - 1)
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert not caught.value.write_attempted and not caught.value.retryable
    assert not writes(system)


def test_update_older_than_fourteen_days_stops_without_replacement_card(cards):
    system = cards()
    old = str(int((utcnow() - timedelta(days=14, seconds=1)).timestamp() * 1000))
    system.receipt["create_time"] = old
    system.identity.source.card_receipt["create_time"] = old
    system.remote.message["create_time"] = old
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_card_expired" and not caught.value.retryable
    assert not writes(system)


@pytest.mark.parametrize("phase", ["token", "get"])
def test_owner_is_rechecked_after_network_preflight(cards, phase):
    system = cards()
    def replace():
        system.identity.owner = "ou_replaced"
    if phase == "token":
        system.remote.token_hook = replace
    else:
        system.remote.get_hook = replace
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_owner_changed" and not writes(system)


def test_receipt_is_rechecked_after_bot_get(cards):
    system = cards()
    system.remote.get_hook = lambda: setattr(system.identity.source, "card_create_state", "unknown")
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_card_unverified" and not writes(system)


def test_read_failure_is_safe_to_retry_and_does_not_mark_patch_attempted(cards):
    system = cards()
    system.remote.failure = "get_timeout"
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.retryable and caught.value.write_attempted is False
    assert len([request for request in system.remote.calls if request.method == "GET"]) == 3
    assert not writes(system)


def test_deleted_error_during_verification_is_permanent_before_patch(cards):
    system = cards()
    system.remote.failure = "get_deleted"
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_card_deleted" and not caught.value.retryable
    assert not writes(system)


@pytest.mark.parametrize("operation", ["send", "update"])
@pytest.mark.parametrize("failure", ["write_timeout", "write_expired_token", "write_rate_limit", "write_server_error"])
def test_outbound_card_write_is_not_retried_or_mislabeled_preflight(cards, operation, failure):
    system = cards()
    system.remote.failure = failure
    with pytest.raises(FeishuError) as caught:
        if operation == "send":
            system.client.send_self_status_card(card(), "status-id", expected_owner="ou_owner")
        else:
            update(system)
    assert not isinstance(caught.value, StatusCardPreflightError)
    assert not hasattr(caught.value, "write_attempted") and len(writes(system)) == 1


@pytest.mark.parametrize("invalid", [
    None, {}, {"config": {"update_multi": False}},
    {"config": {"update_multi": True}, "text": "字" * 11000},
    {"config": {"update_multi": True}, "text": "\ud800"},
])
def test_invalid_or_oversized_card_fails_before_network(cards, invalid):
    system = cards()
    with pytest.raises(StatusCardPreflightError) as caught:
        system.client.send_self_status_card(invalid, "status-id", expected_owner="ou_owner")
    assert caught.value.code == "invalid_status_card" and not caught.value.retryable
    assert not system.remote.calls


@pytest.mark.parametrize("scope", ["im:message:send", "im:message:readonly", "im:message:send_as_bot"])
def test_update_needs_bot_read_and_update_permissions(cards, scope):
    system = cards(scopes=scope)
    with pytest.raises(StatusCardPreflightError) as caught:
        update(system)
    assert caught.value.code == "status_card_permission_required" and caught.value.retryable
    assert not writes(system)


def test_invalid_send_receipt_is_ambiguous_after_one_post(cards):
    system = cards()
    system.remote.message.pop("sender")
    with pytest.raises(FeishuError):
        system.client.send_self_status_card(card(), "status-id", expected_owner="ou_owner")
    assert len(writes(system)) == 1


def test_invalid_patch_response_is_ambiguous_after_one_patch(cards):
    system = cards()
    system.remote.patch_data = None
    with pytest.raises(FeishuError):
        update(system)
    assert len(writes(system)) == 1
