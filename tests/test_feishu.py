import json

import httpx
import pytest

from app.errors import FeishuError
from app.feishu import FeishuClient


def test_reads_retry_rate_limits_but_token_exchange_does_not(settings):
    calls = []
    waits = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "999"}, json={"code": 99991400})
        return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_test"}})

    client = FeishuClient(
        settings, None, None, transport=httpx.MockTransport(handler), sleep=waits.append
    )
    assert client.profile("access") == {"open_id": "ou_test"}
    assert waits == [5, 5]
    assert calls[0].url.path == "/open-apis/authen/v1/user_info"
    calls.clear()
    with pytest.raises(FeishuError):
        client.poll_device_token("temporary-device-code")
    assert len(calls) == 1
    assert calls[0].url.path == "/oauth/v3/token"
    body = json.loads(calls[0].content)
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert body["device_code"] == "temporary-device-code"
    assert "redirect_uri" not in body
    client.close()


def test_provider_error_never_echoes_secrets(settings):
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"code": 99991679, "msg": "secret: leaked-access-token"})
    )
    client = FeishuClient(settings, None, None, transport=transport)
    with pytest.raises(FeishuError) as error:
        client.profile("test")
    assert "leaked-access-token" not in str(error.value)
    assert error.value.code == "feishu_99991679"
    client.close()


@pytest.mark.parametrize("body", [[], None, "unexpected", {}])
def test_malformed_provider_payload_is_rejected(settings, body):
    client = FeishuClient(
        settings,
        None,
        None,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with pytest.raises(FeishuError):
        client.profile("test")
    client.close()
