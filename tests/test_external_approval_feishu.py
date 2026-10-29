"""Reconcile known reminders from the existing personal approval permission."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.errors import AppError, FeishuError
from app.feishu import FeishuClient
from app.models import Account

START = "1793203200"
END = "1793289600"


@pytest.fixture
def personal(settings):
    state = SimpleNamespace(
        owner="ou_owner", calls=[], token_hook=None, read_hook=None,
        response={"code": 0, "data": {"tasks": [], "has_more": False}},
    )

    @contextmanager
    def sessions():
        def get(model, identifier):
            assert model is Account and identifier == 1
            return SimpleNamespace(open_id=state.owner) if state.owner else None
        yield SimpleNamespace(get=get)

    def handler(request):
        state.calls.append(request)
        assert request.url.path == "/open-apis/approval/v4/tasks"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer user-token"
        assert not request.content
        if state.read_hook:
            state.read_hook()
        return httpx.Response(200, json=state.response)

    def token(*args, **kwargs):
        if state.token_hook:
            state.token_hook()
        return "user-token"

    client = FeishuClient(settings, None, sessions, transport=httpx.MockTransport(handler))
    client.access_token = token
    client.app_token = lambda: pytest.fail("Personal completion must never acquire a tenant token")
    yield SimpleNamespace(client=client, state=state)
    client.close()


def handled(system, **kwargs):
    return system.client.personal_handled_approval_tasks(
        "definition_target", expected_owner="ou_owner",
        start_timestamp=START, end_timestamp=END, **kwargs,
    )


@pytest.mark.parametrize("cursor", [None, "next-page"])
def test_handled_tasks_use_only_personal_token_and_bounded_definition_window(personal, cursor):
    row = {"task_id": "task_target", "instance_code": "instance_target",
           "user_id": "ou_owner", "status": 2, "topic": 2, "instance_status": 2}
    personal.state.response["data"]["tasks"] = [row]
    assert handled(personal, page_token=cursor) == personal.state.response["data"]
    assert len(personal.state.calls) == 1
    assert dict(personal.state.calls[0].url.params) == {
        "topic": "2", "definition_code": "definition_target", "locale": "zh-CN",
        "user_id_type": "open_id", "page_size": "100",
        "start_timestamp": START, "end_timestamp": END,
        **({"page_token": cursor} if cursor else {}),
    }
    assert not hasattr(personal.client, "external_approval_tasks")


@pytest.mark.parametrize("owner", [None, "ou_other"])
def test_wrong_or_disconnected_owner_does_not_call_remote_api(personal, owner):
    personal.state.owner = owner
    with pytest.raises(AppError):
        handled(personal)
    assert not personal.state.calls


@pytest.mark.parametrize("phase", ["token", "read"])
def test_changed_owner_during_read_prevents_returning_foreign_results(personal, phase):
    setattr(personal.state, f"{phase}_hook", lambda: setattr(personal.state, "owner", "ou_replaced"))
    with pytest.raises(AppError) as caught:
        handled(personal)
    assert caught.value.code == "approval_owner_changed"


@pytest.mark.parametrize("changes", [
    {"definition_code": ""}, {"definition_code": None}, {"definition_code": "  "},
    {"expected_owner": ""}, {"expected_owner": "other"},
    {"page_token": ""}, {"page_token": 12},
    {"start_timestamp": ""}, {"end_timestamp": ""}, {"start_timestamp": None},
    {"start_timestamp": 1793203200}, {"start_timestamp": "-1"},
    {"start_timestamp": "0"}, {"start_timestamp": "1.0"},
    {"start_timestamp": END}, {"end_timestamp": START},
    {"end_timestamp": str(int(START) + 14 * 86400 + 1)},
])
def test_required_filters_cannot_be_removed_or_broadened(personal, changes):
    arguments = dict(definition_code="definition_target", expected_owner="ou_owner",
                     start_timestamp=START, end_timestamp=END)
    arguments.update(changes)
    with pytest.raises(AppError):
        personal.client.personal_handled_approval_tasks(**arguments)
    assert not personal.state.calls


@pytest.mark.parametrize("data", [None, [], {}, {"tasks": None, "has_more": False},
    {"tasks": [], "has_more": "false"}, {"tasks": [], "has_more": True},
    {"tasks": [None], "has_more": False},
    {"tasks": [], "has_more": True, "page_token": "same-page"},
])
def test_unverifiable_handled_page_is_rejected(personal, data):
    personal.state.response = {"code": 0, "data": data}
    with pytest.raises(FeishuError):
        handled(personal, page_token="same-page")


def test_existing_user_permission_error_keeps_code_without_provider_details(personal):
    personal.state.response = {
        "code": 99991672, "msg": "secret provider details",
        "error": {"permission_violations": [{"subject": "secret permission details"}]},
    }
    with pytest.raises(FeishuError) as caught:
        handled(personal)
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
