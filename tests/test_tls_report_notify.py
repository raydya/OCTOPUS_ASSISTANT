import hashlib
import io
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import tls_report_notify as notify

DAY = "2026-10-21"
SUCCESS = {"status": "published", "errors": 2345, "services": 12,
           "url": "https://atrenew.feishu.cn/wiki/reportnode", "document_id": "reportdoc",
           "content_hash": "a" * 64}
IDENTITY = {"owner": "ou_owner", "identity": "b" * 64}


class FakeTransport:
    def __init__(self, folder):
        self.folder = folder
        self.sent = []
        self.prepared = 0
        self.prepare_error = None
        self.send_error = None
        self.identity = dict(IDENTITY)

    def prepare(self):
        self.prepared += 1
        if self.prepare_error:
            raise self.prepare_error
        return self.identity

    def send(self, text, identifier, identity):
        states = [json.loads(path.read_text()) for path in self.folder.glob("*.json")]
        state = next(state for state in states if state["uuid"] == identifier)
        assert state["submitted_at"] and state["status"] == "sending"
        assert state["identity"] == identity
        self.sent.append((text, identifier, identity))
        if self.send_error:
            raise self.send_error
        return {"message_id": f"om_message_{len(self.sent)}"}


@pytest.fixture
def notification(tmp_path):
    transport = FakeTransport(tmp_path)

    def run(result=None):
        return notify.notify_report(DAY, result or SUCCESS, state_dir=tmp_path, transport=transport)

    return transport, run, tmp_path


def test_success_contains_only_date_statistics_and_document_link(notification):
    transport, run, folder = notification
    result = run()
    assert result["status"] == "sent" and result["message_id"] == "om_message_1"
    text, identifier, identity = transport.sent[0]
    assert DAY in text and "12 个服务" in text and "2,345 条错误" in text
    assert SUCCESS["url"] in text
    assert "localhost" not in text and "工作台" not in text
    assert identity == IDENTITY and len(identifier) <= 50
    assert not (set(result) & {"owner", "identity", "text"})
    assert next(folder.glob("*.json")).stat().st_mode & 0o777 == 0o600


def test_same_revision_deduplicates_even_with_changed_status_and_published_time(notification):
    transport, run, _ = notification
    run()
    second = run({**SUCCESS, "status": "already_published", "published_at": "later"})
    assert second["status"] == "sent"
    assert len(transport.sent) == 1 and transport.prepared == 1


def test_same_day_new_content_revision_notifies_once(notification):
    transport, run, _ = notification
    run()
    amended = {**SUCCESS, "content_hash": "c" * 64}
    assert run(amended)["status"] == "sent"
    run(amended)
    assert len(transport.sent) == 2
    assert transport.sent[0][1] != transport.sent[1][1]


def test_same_day_different_document_is_a_separate_revision(notification):
    transport, run, _ = notification
    run()
    run({**SUCCESS, "document_id": "anotherdoc", "url": "https://atrenew.feishu.cn/wiki/anothernode"})
    assert len(transport.sent) == 2


def test_failure_once_per_day_then_success_is_notified(notification):
    transport, run, _ = notification
    failure = {"status": "failed", "error_type": "SourceError", "error": "secret token"}
    assert run(failure)["status"] == "sent"
    run({**failure, "error_type": "PublishError"})
    assert len(transport.sent) == 1
    assert "TLS 日志查询未完成" in transport.sent[0][0]
    assert "secret" not in transport.sent[0][0] and "工作台" not in transport.sent[0][0]
    assert run()["status"] == "sent"
    assert len(transport.sent) == 2


def test_failure_text_uses_allowlist_not_exception_message():
    _, text = notify.report_message(DAY, {"status": "failed", "error_type": "password=private"})
    assert "任务未完成" in text and "password" not in text and "private" not in text


def test_preflight_failure_can_retry_without_ambiguous_submission(notification):
    transport, run, folder = notification
    transport.prepare_error = notify.NotificationError("self_message_permission_required")
    result = run()
    assert result["status"] == "failed" and "submitted_at" not in result
    assert not transport.sent
    state = json.loads(next(folder.glob("*.json")).read_text())
    assert "submitted_at" not in state
    transport.prepare_error = None
    assert run()["status"] == "sent"
    assert len(transport.sent) == 1


def test_send_timeout_is_unknown_and_never_resent(notification):
    transport, run, _ = notification
    transport.send_error = TimeoutError("token=private")
    result = run()
    assert result["status"] == "unknown" and "private" not in json.dumps(result)
    transport.send_error = None
    assert run()["status"] == "unknown"
    assert len(transport.sent) == 1 and transport.prepared == 1


def test_interrupted_process_is_not_resent(notification):
    transport, run, _ = notification
    transport.send_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run()
    transport.send_error = None
    result = run()
    assert result["status"] == "unknown"
    assert result["error_code"] == "notification_interrupted"
    assert len(transport.sent) == 1


@pytest.mark.parametrize("status", ["collected", "already_running"])
def test_non_completion_does_not_prepare_or_send(notification, status):
    transport, run, folder = notification
    assert run({"status": status})["status"] == "skipped"
    assert transport.prepared == 0 and not transport.sent
    assert not list(folder.glob("*.json"))


@pytest.mark.parametrize("changes", [
    {"url": "http://localhost:8180"}, {"url": "https://atrenew.feishu.cn.evil/wiki/token"},
    {"url": "https://atrenew.feishu.cn/wiki/token?secret=private"},
    {"url": "https://someone@atrenew.feishu.cn/wiki/token"},
    {"errors": -1}, {"services": True}, {"content_hash": "bad"}, {"document_id": ""},
])
def test_invalid_success_fails_before_sending(notification, changes):
    transport, run, _ = notification
    with pytest.raises(notify.NotificationError):
        run({**SUCCESS, **changes})
    assert transport.prepared == 0 and not transport.sent


def test_corrupt_state_is_not_replaced_by_a_fresh_send(notification):
    transport, run, folder = notification
    transport.prepare_error = notify.NotificationError("not_configured")
    run()
    next(folder.glob("*.json")).write_text("not json")
    transport.prepare_error = None
    with pytest.raises(notify.NotificationError, match="state_unreadable"):
        run()
    assert not transport.sent


def test_transport_uses_existing_app_and_stdin_without_shell(monkeypatch):
    process = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
        "ok": True, **IDENTITY,
    }), ""))
    monkeypatch.setattr(notify.subprocess, "run", process)
    transport = notify.AppBotTransport()
    assert transport.prepare() == IDENTITY
    args = process.call_args.args[0]
    assert args[:2] == ["docker", "compose"]
    assert ["exec", "-T", "app", "python", "-c"] == args[6:11]
    assert args[-1] == notify.BRIDGE
    assert json.loads(process.call_args.kwargs["input"]) == {"mode": "prepare"}
    assert "shell" not in process.call_args.kwargs


def test_transport_failure_suppresses_docker_stderr_and_credentials(monkeypatch):
    process = Mock(return_value=subprocess.CompletedProcess([], 1, "", "password=private"))
    monkeypatch.setattr(notify.subprocess, "run", process)
    with pytest.raises(notify.NotificationError) as error:
        notify.AppBotTransport().prepare()
    assert "private" not in str(error.value)


def test_transport_rejects_fake_send_response(monkeypatch):
    process = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true,"message_id":"wrong"}', ""))
    monkeypatch.setattr(notify.subprocess, "run", process)
    with pytest.raises(notify.NotificationError, match="invalid_response"):
        notify.AppBotTransport().send("text", "uuid", IDENTITY)


def execute_bridge(monkeypatch, capsys, request, *, count=1):
    import app.config
    import app.db
    import app.feishu
    import app.security

    account = SimpleNamespace(open_id="ou_owner", tenant_key="tenant")
    db = Mock()
    db.get.return_value = account
    db.scalar.return_value = count
    context = Mock()
    context.__enter__ = Mock(return_value=db)
    context.__exit__ = Mock(return_value=False)
    engine = Mock()
    client = Mock()
    client.self_message_permissions.return_value = {"ready": True}
    client.send_self_status.return_value = {"message_id": "om_realbot"}
    monkeypatch.setattr(app.config, "Settings", lambda: SimpleNamespace(feishu_app_id="cli_octopus"))
    monkeypatch.setattr(app.db, "make_database", lambda settings: (engine, lambda: context))
    monkeypatch.setattr(app.security, "Security", lambda *args: object())
    monkeypatch.setattr(app.feishu, "FeishuClient", lambda *args: client)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    exec(compile(notify.BRIDGE, "<notification-bridge>", "exec"), {})
    return json.loads(capsys.readouterr().out), client


def test_bridge_prepare_is_read_only_and_resolves_account_one(monkeypatch, capsys):
    result, client = execute_bridge(monkeypatch, capsys, {"mode": "prepare"})
    assert result["ok"] and result["owner"] == "ou_owner"
    assert len(result["identity"]) == 64
    client.send_self_status.assert_not_called()
    client.close.assert_called_once()


def test_bridge_sends_through_existing_bot_with_expected_owner(monkeypatch, capsys):
    identity = hashlib.sha256(json.dumps(["cli_octopus", "ou_owner", "tenant"]).encode()).hexdigest()
    request = {"mode": "send", "text": "日报完成", "uuid": "uuid", "owner": "ou_owner", "identity": identity}
    result, client = execute_bridge(monkeypatch, capsys, request)
    assert result == {"ok": True, "message_id": "om_realbot"}
    client.send_self_status.assert_called_once_with("日报完成", "uuid", expected_owner="ou_owner")


def test_bridge_rejects_recipient_or_app_identity_change(monkeypatch, capsys):
    request = {"mode": "send", "text": "日报完成", "uuid": "uuid", "owner": "ou_other", "identity": "wrong"}
    result, client = execute_bridge(monkeypatch, capsys, request)
    assert result == {"ok": False, "error_code": "status_owner_changed"}
    client.send_self_status.assert_not_called()


def test_bridge_requires_unique_bound_account(monkeypatch, capsys):
    result, client = execute_bridge(monkeypatch, capsys, {"mode": "prepare"}, count=2)
    assert result == {"ok": False, "error_code": "connect_required"}
    client.send_self_status.assert_not_called()
