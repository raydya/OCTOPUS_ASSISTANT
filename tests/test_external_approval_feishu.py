"""Read one owner's third-party approval tasks without collecting history."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.errors import AppError, FeishuError
from app.feishu import FeishuClient
from app.models import Account


@pytest.fixture
def external(settings):
    state = SimpleNamespace(
        owner="ou_owner", calls=[], token_hook=None, search_hook=None,
        response={"code": 0, "data": {"task_list": [], "has_more": False}},
    )

    @contextmanager
    def sessions():
        def get(model, identifier):
            assert model is Account and identifier == 1
            return SimpleNamespace(open_id=state.owner) if state.owner else None

        yield SimpleNamespace(get=get)

    def handler(request):
        state.calls.append(request)
        if request.url.path == "/oauth/v3/token":
            assert json.loads(request.content)["grant_type"] == "client_credentials"
            if state.token_hook:
                state.token_hook()
            return httpx.Response(200, json={
                "code": 0, "access_token": "tenant-token", "expires_in": 7200,
            })
        assert request.url.path == "/open-apis/approval/v4/tasks/search"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer tenant-token"
        if state.search_hook:
            state.search_hook()
        return httpx.Response(200, json=state.response)

    client = FeishuClient(settings, None, sessions, transport=httpx.MockTransport(handler))
    client.access_token = lambda *args, **kwargs: pytest.fail("Search must use the tenant token")
    yield SimpleNamespace(client=client, state=state)
    client.close()


def search(system, **kwargs):
    return system.client.external_approval_tasks("instance_target", expected_owner="ou_owner", **kwargs)


@pytest.mark.parametrize("cursor", [None, "next-page"])
def test_search_is_filtered_to_bound_owner_and_exact_instance(external, cursor):
    row = {
        "approval": {"code": "definition", "is_external": True},
        "instance": {"code": "instance_target", "status": "pending"},
        "task": {"task_id": "task_target", "user_id": "ou_owner", "status": "approved"},
    }
    external.state.response["data"]["task_list"] = [row]

    assert search(external, page_token=cursor) == external.state.response["data"]

    request = external.state.calls[-1]
    assert dict(request.url.params) == {
        "user_id_type": "open_id", "page_size": "100",
        **({"page_token": cursor} if cursor else {}),
    }
    assert json.loads(request.content) == {
        "user_id": "ou_owner", "instance_code": "instance_target", "locale": "zh-CN",
    }
    assert len(external.state.calls) == 2


@pytest.mark.parametrize("owner", [None, "ou_other"])
def test_wrong_or_disconnected_owner_does_not_call_remote_api(external, owner):
    external.state.owner = owner
    with pytest.raises(AppError):
        search(external)
    assert not external.state.calls


@pytest.mark.parametrize("phase", ["token", "search"])
def test_changed_owner_after_network_prevents_returning_foreign_results(external, phase):
    setattr(external.state, f"{phase}_hook", lambda: setattr(external.state, "owner", "ou_replaced"))
    with pytest.raises(AppError) as caught:
        search(external)
    assert caught.value.code == "approval_owner_changed"
    assert len(external.state.calls) == (1 if phase == "token" else 2)


@pytest.mark.parametrize("instance,owner,cursor", [
    ("", "ou_owner", None), (None, "ou_owner", None), ("  ", "ou_owner", None),
    ("instance_target", "", None), ("instance_target", "ou_owner", ""),
    ("instance_target", "ou_owner", 12),
])
def test_invalid_filter_cannot_turn_into_broad_search(external, instance, owner, cursor):
    with pytest.raises(AppError):
        external.client.external_approval_tasks(instance, expected_owner=owner, page_token=cursor)
    assert not external.state.calls


@pytest.mark.parametrize("data", [None, [], {}, {"task_list": None, "has_more": False},
    {"task_list": [], "has_more": "false"}, {"task_list": [], "has_more": True},
    {"task_list": [], "has_more": True, "page_token": "same-page"},
])
def test_unverifiable_search_page_is_rejected(external, data):
    external.state.response = {"code": 0, "data": data}
    with pytest.raises(FeishuError) as caught:
        search(external, page_token="same-page")
    assert caught.value.code == "feishu_invalid_external_approval_tasks"


def test_search_permission_error_keeps_numeric_code_without_provider_details(external):
    external.state.response = {
        "code": 99991672, "msg": "secret provider details",
        "error": {"permission_violations": [{"subject": "secret permission details"}]},
    }
    with pytest.raises(FeishuError) as caught:
        search(external)
    assert caught.value.code == "feishu_99991672"
    assert "secret" not in str(caught.value)


def test_numeric_provider_error_takes_precedence_over_nested_error(settings):
    client = FeishuClient(settings, None, None, transport=httpx.MockTransport(
        lambda _: httpx.Response(400, json={
            "code": 99991672, "msg": "secret", "error": {"details": "secret"},
        })
    ))
    try:
        with pytest.raises(FeishuError) as caught:
            client.profile("test")
        assert caught.value.code == "feishu_99991672"
        assert "secret" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("code", [None, 0, 12345])
@pytest.mark.parametrize("named_error", ["invalid_grant", "authorization_pending", "slow_down"])
def test_oauth_named_error_remains_classifiable(settings, code, named_error):
    client = FeishuClient(settings, None, None, transport=httpx.MockTransport(
        lambda _: httpx.Response(400, json={
            **({"code": code} if code is not None else {}),
            "error": named_error, "error_description": "secret",
        })
    ))
    try:
        with pytest.raises(FeishuError) as caught:
            client.poll_device_token("temporary")
        assert caught.value.code == f"feishu_{named_error}"
        assert "secret" not in str(caught.value)
    finally:
        client.close()
