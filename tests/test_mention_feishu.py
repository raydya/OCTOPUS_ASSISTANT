import json

import httpx
import pytest

from app.errors import AppError, FeishuError
from app.feishu import FeishuClient

START = "2026-10-23T12:00:00+08:00"
END = "2026-10-23T12:10:00+08:00"


def search_data(**overrides):
    return {
        "items": [{"id": "om_one", "meta_data": {"message_id": "om_one", "chat_id": "oc_one"}}],
        "has_more": False,
        "total": 1,
        **overrides,
    }


@pytest.fixture
def make_client(settings):
    clients = []

    def factory(handler):
        client = FeishuClient(
            settings, None, None, transport=httpx.MockTransport(handler), sleep=lambda _: None
        )
        client.access_token = lambda rejected_token=None: "user-access"

        def forbid_bot(*args, **kwargs):
            pytest.fail("Personal message reads must never obtain an app/bot token")

        client.app_token = forbid_bot
        client._token = forbid_bot
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


def test_search_mentions_uses_user_search_filter_and_preserves_notice(make_client):
    calls = []
    payload = search_data(
        has_more=True,
        page_token="next-page",
        notice="Results may be incomplete",
    )

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": payload})

    client = make_client(handler)
    assert client.search_mentions(START, END, "current-page") == payload
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/open-apis/im/v1/messages/search"
    assert dict(request.url.params) == {
        "page_size": "30", "page_token": "current-page", "user_id_type": "open_id"
    }
    assert request.headers["Authorization"] == "Bearer user-access"
    assert json.loads(request.content) == {
        "query": "",
        "filter": {"is_at_me": True, "time_range": {"start_time": START, "end_time": END}},
    }


def test_search_empty_page_with_zero_results_is_valid(make_client):
    payload = search_data(items=[], total=0, page_token="")
    client = make_client(lambda _: httpx.Response(200, json={"code": 0, "data": payload}))
    assert client.search_mentions("2026-10-23T04:00:00Z", "2026-10-23T04:10:00Z") == payload


@pytest.mark.parametrize("start,end", [
    (None, END),
    ("2026-10-23T12:00:00", END),
    ("2026-10-23T12:00:00.001+08:00", END),
    ("2026-02-30T12:00:00+08:00", END),
    ("2026-10-23T25:00:00+08:00", END),
    (END, START),
    (END, END),
    ("2026-10-23T04:10:00Z", END),
    (START, 123),
])
def test_search_rejects_invalid_or_empty_time_windows_without_network(make_client, start, end):
    client = make_client(lambda _: pytest.fail("Invalid input must not reach Feishu"))
    with pytest.raises(AppError, match="带时区"):
        client.search_mentions(start, end)


@pytest.mark.parametrize("page_token", ["", 1, [], {}])
def test_search_rejects_invalid_page_token_without_network(make_client, page_token):
    client = make_client(lambda _: pytest.fail("Invalid input must not reach Feishu"))
    with pytest.raises(AppError) as error:
        client.search_mentions(START, END, page_token)
    assert error.value.code == "invalid_page_token"


@pytest.mark.parametrize("payload", [
    [],
    None,
    {},
    search_data(items=None),
    search_data(items=[None]),
    search_data(items=[{"meta_data": []}]),
    search_data(items=[{"meta_data": {"message_id": "not-a-message"}}]),
    search_data(items=[{"meta_data": {"message_id": []}}]),
    search_data(has_more=1),
    search_data(has_more=True),
    search_data(has_more=True, page_token=""),
    search_data(has_more=True, page_token="current-page"),
    search_data(page_token="unexpected-continuation"),
    search_data(page_token=123),
    search_data(notice={"secret": "provider-detail"}),
    search_data(total=True),
    search_data(total=-1),
])
def test_search_rejects_ambiguous_pagination_and_malformed_data(make_client, payload):
    client = make_client(lambda _: httpx.Response(200, json={"code": 0, "data": payload}))
    with pytest.raises(FeishuError) as error:
        client.search_mentions(START, END, "current-page")
    assert "provider-detail" not in str(error.value)


def test_mget_uses_repeated_parameters_and_retains_raw_message_fields(make_client):
    calls = []
    rows = [{
        "message_id": "om_one", "chat_id": "oc_one", "create_time": "1792728000000",
        "sender": {"id": "ou_sender", "sender_type": "user", "name": "Sender"},
        "mentions": [{"id": "ou_owner", "key": "@_user_1"}],
        "body": {"content": '{"text":"@_user_1 urgent"}'},
    }]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"items": rows}})

    client = make_client(handler)
    # Missing IDs are intentionally visible to the caller for checkpoint validation.
    assert client.messages_by_ids(["om_one", "om_two"]) == rows
    request = calls[0]
    assert request.method == "GET"
    assert request.url.path == "/open-apis/im/v1/messages/mget"
    assert request.url.params.get_list("message_ids") == ["om_one", "om_two"]
    assert request.url.params["card_msg_content_type"] == "raw_card_content"
    assert request.url.params["with_sender_name"] == "true"
    assert request.headers["Authorization"] == "Bearer user-access"


@pytest.mark.parametrize("message_ids", [
    [], None, "om_one", ("om_one",), ["om_one", "om_one"], [None], [{}],
    ["oc_wrong"], ["om_one/../../token"], [f"om_{number}" for number in range(51)],
])
def test_mget_rejects_invalid_ids_before_network(make_client, message_ids):
    client = make_client(lambda _: pytest.fail("Invalid input must not reach Feishu"))
    with pytest.raises(AppError) as error:
        client.messages_by_ids(message_ids)
    assert error.value.code == "invalid_message_ids"


@pytest.mark.parametrize("items", [
    None, {}, [None], [{"message_id": []}], [{"message_id": "om_other"}],
    [{"message_id": "om_one"}, {"message_id": "om_one"}],
])
def test_mget_rejects_malformed_duplicate_or_unrequested_rows(make_client, items):
    client = make_client(
        lambda _: httpx.Response(200, json={"code": 0, "data": {"items": items}})
    )
    with pytest.raises(FeishuError):
        client.messages_by_ids(["om_one"])


def test_mget_accepts_fifty_ids_and_empty_missing_result(make_client):
    client = make_client(
        lambda _: httpx.Response(200, json={"code": 0, "data": {"items": []}})
    )
    assert client.messages_by_ids([f"om_{number}" for number in range(50)]) == []


def test_chat_binds_metadata_to_requested_id(make_client):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/open-apis/im/v1/chats/oc_one"
        assert request.url.params["user_id_type"] == "open_id"
        assert request.headers["Authorization"] == "Bearer user-access"
        return httpx.Response(200, json={"code": 0, "data": {"name": "Team", "chat_type": "group"}})

    client = make_client(handler)
    assert client.chat("oc_one") == {"name": "Team", "chat_type": "group", "chat_id": "oc_one"}


@pytest.mark.parametrize("chat_id", [None, [], "", "om_one", "oc_one/../../token"])
def test_chat_rejects_invalid_id_without_network(make_client, chat_id):
    client = make_client(lambda _: pytest.fail("Invalid input must not reach Feishu"))
    with pytest.raises(AppError) as error:
        client.chat(chat_id)
    assert error.value.code == "invalid_chat_id"


@pytest.mark.parametrize("data", [[], None, {}, {"chat_id": "oc_other", "name": "Secret"}])
def test_chat_rejects_empty_or_contradictory_metadata(make_client, data):
    client = make_client(lambda _: httpx.Response(200, json={"code": 0, "data": data}))
    with pytest.raises(FeishuError) as error:
        client.chat("oc_one")
    assert "Secret" not in str(error.value)


READ_CASES = [
    ("search_mentions", (START, END), search_data()),
    ("messages_by_ids", (["om_one"],), {"items": [{"message_id": "om_one"}]}),
    ("chat", ("oc_one",), {"name": "Team"}),
]


@pytest.mark.parametrize("method,args,payload", READ_CASES)
def test_reads_refresh_rejected_user_token_once(make_client, method, args, payload):
    calls = []
    token_requests = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "expired-user-access"})
        return httpx.Response(200, json={"code": 0, "data": payload})

    def access_token(rejected_token=None):
        token_requests.append(rejected_token)
        return "renewed-user-access" if rejected_token else "old-user-access"

    client = make_client(handler)
    client.access_token = access_token
    getattr(client, method)(*args)
    assert token_requests == [None, "old-user-access"]
    assert [request.headers["Authorization"] for request in calls] == [
        "Bearer old-user-access", "Bearer renewed-user-access"
    ]
    assert calls[0].content == calls[1].content
    assert calls[0].url == calls[1].url


@pytest.mark.parametrize("method,args,payload", READ_CASES)
@pytest.mark.parametrize("code,expected_calls", [(99991663, 2), (99991679, 1)])
def test_reads_never_retry_permission_denial_or_refresh_repeatedly(
    make_client, method, args, payload, code, expected_calls
):
    calls = []
    token_requests = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": code, "msg": "secret provider user-token"})

    def access_token(rejected_token=None):
        token_requests.append(rejected_token)
        return "user-access"

    client = make_client(handler)
    client.access_token = access_token
    with pytest.raises(FeishuError) as error:
        getattr(client, method)(*args)
    assert len(calls) == expected_calls
    assert len(token_requests) == expected_calls
    assert "secret" not in str(error.value)
    assert "user-token" not in str(error.value)


def test_search_network_error_is_safe_and_does_not_refresh_token(make_client):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("secret user-token", request=request)

    client = make_client(handler)
    with pytest.raises(FeishuError) as error:
        client.search_mentions(START, END)
    assert error.value.provider_code == "network"
    assert "secret" not in str(error.value)
    assert len(calls) == 1
