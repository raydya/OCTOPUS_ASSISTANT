import pytest
from conftest import login
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", [
    "/api/sync/chats",
    "/api/sync/messages/oc_one",
    "/api/sync/threads/omt_one",
])
def test_reminder_only_rejects_manual_message_sync_before_any_remote_read(make_app, settings, path):
    settings.reminders_only = True

    def forbidden(_request):
        pytest.fail("Disabled chat/history sync must not call Feishu")

    app = make_app(forbidden)
    with TestClient(app, base_url="http://localhost:8000") as client:
        headers = login(client)
        assert client.get("/api/status").json()["reminders_only"] is True
        response = client.post(path, json={"days": 365, "restart": True}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "message_sync_disabled"
