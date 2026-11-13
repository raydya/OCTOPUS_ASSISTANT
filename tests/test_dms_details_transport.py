"""DMS read-only boundaries use fake CLI/API responses and local subprocesses."""

import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import dms_api
from app.dms_api import DmsCliReader, _run_bounded, order_id, validate_snapshot
from app.dms_client import DmsDetailClient
from app.dms_host import DmsHostConfig, create_dms_host_app
from app.dms_review import order_reference
from app.errors import AppError


def snapshot(**changes):
    return {
        "order_id": "123", "base": {"OrderId": 123, "PluginType": "DC_COMMON"},
        "detail": {"OrderDetail": {"ExeSQL": "UPDATE t SET a=1 WHERE id=2"}},
        "sql_checks": [], "complete": True, "errors": [], **changes,
    }


def reader_with(*, plugin="DC_COMMON", pages=None, fail=None, sql_type="TEXT", base_id=123, revision="2026-11-13 12:30:45"):
    calls = []

    def run(argv, timeout):
        assert timeout > 0
        calls.append(argv)
        action = argv[argv.index("dms-enterprise") + 1]
        if action == fail:
            raise dms_api.dms_error()
        if action == "get-order-base-info":
            response = {"OrderBaseInfo": {
                "OrderId": base_id, "PluginType": plugin, "LastModifyTime": revision,
            }}
        elif action == "get-data-correct-order-detail":
            response = {"DataCorrectOrderDetail": {
                "OrderDetail": {"ExeSQL": "UPDATE t SET a=1 WHERE id=2", "SqlType": sql_type},
                "DatabaseList": {"Database": [{"DbId": 42, "EnvType": "test"}]},
                "PreCheckDetail": {"TaskCheckDO": [{"CheckStep": "SQL_PARSE", "CheckStatus": "SUCCESS"}]},
            }}
        elif action == "list-data-correct-pre-check-sql":
            page = int(argv[argv.index("--page-number") + 1])
            response = {"PreCheckSQLList": (pages or {1: []})[page]}
        elif action == "get-perm-apply-order-detail":
            response = {"PermApplyOrderDetail": {"PermType": 4, "Seconds": 3600}}
        elif action == "get-data-export-order-detail":
            response = {"DataExportOrderDetail": {"OrderDetail": {"ExeSQL": "SELECT * FROM t"}}}
        else:
            pytest.fail(f"Unexpected or mutating action: {action}")
        return json.dumps({"Success": True, **response}).encode()

    return DmsCliReader(binary="/fake/aliyun", profile="company-cli-user", tenant_id="456", run=run), calls


def test_detail_reader_binds_order_and_reads_every_sql_page():
    page = [{"DbId": 42, "CheckSQL": f"UPDATE t SET a=1 WHERE id={i}"} for i in range(100)]
    reader, calls = reader_with(pages={1: page, 2: [{"DbId": 42, "CheckSQL": "UPDATE t SET a=2 WHERE id=101"}]})
    result = reader.fetch("123")
    assert result["complete"] is True
    assert len(result["sql_checks"]) == 101
    assert result["detail"]["DatabaseList"]["Database"][0]["EnvType"] == "test"
    assert len(calls) == 5
    for call in calls:
        assert call[:3] == ["/fake/aliyun", "--profile", "company-cli-user"]
        assert call[call.index("--order-id") + 1] == "123"
        assert call[call.index("--tid") + 1] == "456"
        assert "--yes" not in call


@pytest.mark.parametrize("plugin,action", [
    ("PERM_APPLY", "get-perm-apply-order-detail"),
    ("DATA_EXPORT", "get-data-export-order-detail"),
])
def test_read_dispatch_uses_verified_api_type(plugin, action):
    reader, calls = reader_with(plugin=plugin)
    result = reader.fetch(123)
    assert result["complete"] is True
    assert len(calls) == 3 and action in calls[1]


def test_unknown_plugin_returns_base_with_explicit_incomplete_state():
    reader, calls = reader_with(plugin="new_plugin")
    result = reader.fetch("123")
    assert result["base"]["OrderId"] == 123
    assert result["complete"] is False
    assert result["errors"] == ["dms_details_unsupported_type"]
    assert len(calls) == 1


@pytest.mark.parametrize("fail", ["get-data-correct-order-detail", "list-data-correct-pre-check-sql"])
def test_partial_failure_preserves_base_but_never_claims_complete(fail):
    reader, _ = reader_with(fail=fail)
    result = reader.fetch("123")
    assert result["complete"] is False
    assert result["errors"] == ["dms_details_unavailable"]


def test_mismatched_order_never_fetches_or_returns_someone_elses_details():
    reader, calls = reader_with(base_id=999)
    with pytest.raises(AppError):
        reader.fetch("123")
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("LastModifyTime", "2026-11-13 16:00:00"), ("PluginType", "PERM_APPLY"), ("StatusCode", "closed")])
def test_changed_order_during_pagination_never_claims_complete(field, value):
    reader, _ = reader_with()
    original = reader.run
    reads = 0

    def mutate(argv, timeout):
        nonlocal reads
        response = json.loads(original(argv, timeout))
        if "get-order-base-info" in argv:
            reads += 1
            if reads == 2:
                response["OrderBaseInfo"][field] = value
        return json.dumps(response).encode()

    reader.run = mutate
    result = reader.fetch("123")
    assert result["complete"] is False
    assert result["errors"] == ["dms_details_changed_during_read"]


@pytest.mark.parametrize("revision", [None, "", " ", "yesterday", "2026-02-30 12:30:45", "2026-11-13 25:30:45", 123, True])
def test_missing_or_invalid_revision_is_not_complete_evidence(revision):
    reader, _ = reader_with(revision=revision)
    result = reader.fetch("123")
    assert result["complete"] is False
    assert result["errors"] == ["dms_details_revision_unverified"]


def test_absent_revision_field_is_not_complete_evidence():
    reader, _ = reader_with()
    original = reader.run

    def omit(argv, timeout):
        response = json.loads(original(argv, timeout))
        if "OrderBaseInfo" in response:
            response["OrderBaseInfo"].pop("LastModifyTime")
        return json.dumps(response).encode()

    reader.run = omit
    result = reader.fetch("123")
    assert result["complete"] is False
    assert result["errors"] == ["dms_details_revision_unverified"]


def test_consistent_valid_api_revision_allows_complete_snapshot():
    reader, _ = reader_with(revision="2024-02-29 00:00:00")
    result = reader.fetch("123")
    assert result["base"]["LastModifyTime"] == "2024-02-29 00:00:00"
    assert result["complete"] is True
    assert result["errors"] == []


def test_identity_mismatch_on_final_read_is_rejected():
    reader, _ = reader_with()
    original = reader.run
    reads = 0

    def mutate(argv, timeout):
        nonlocal reads
        response = json.loads(original(argv, timeout))
        if "get-order-base-info" in argv:
            reads += 1
            if reads == 2:
                response["OrderBaseInfo"]["OrderId"] = 999
        return json.dumps(response).encode()

    reader.run = mutate
    with pytest.raises(AppError) as caught:
        reader.fetch("123")
    assert caught.value.code == "dms_details_invalid_response"


@pytest.mark.parametrize("identifier", [True, False, -1, 0, "01", "123;id", "--help", "https://dms.aliyun.com/", 2**63])
def test_order_ids_cannot_be_commands_urls_or_out_of_range(identifier):
    with pytest.raises(AppError):
        order_id(identifier)


def test_repeated_pages_and_page_caps_are_explicitly_incomplete(monkeypatch):
    page = [{"CheckSQL": f"SELECT {i}"} for i in range(100)]
    reader, _ = reader_with(pages={1: page, 2: page})
    assert reader.fetch("123")["errors"] == ["dms_details_repeated_page"]
    monkeypatch.setattr(dms_api, "MAX_PAGES", 1)
    reader, _ = reader_with(pages={1: page})
    assert reader.fetch("123")["errors"] == ["dms_details_truncated"]


def test_sql_attachment_is_never_claimed_to_have_been_reviewed():
    reader, _ = reader_with(sql_type="attachment")
    result = reader.fetch("123")
    assert result["complete"] is False
    assert "dms_sql_attachment_not_reviewed" in result["errors"]


def test_cli_failure_does_not_disclose_provider_error_or_secrets():
    reader = DmsCliReader(binary="aliyun", run=lambda *_: b'{"Success":false,"ErrorMessage":"private-token"}')
    with pytest.raises(AppError) as caught:
        reader.fetch("123")
    assert "private" not in str(caught.value)


def test_process_output_and_runtime_are_bounded():
    assert _run_bounded([sys.executable, "-c", "print('{}')"], 2).strip() == b"{}"
    with pytest.raises(AppError) as caught:
        _run_bounded([sys.executable, "-c", "import time;time.sleep(10)"], 0.05)
    assert caught.value.code == "dms_details_timeout"
    with pytest.raises(AppError) as caught:
        _run_bounded([sys.executable, "-c", "import sys;sys.stdout.write('x'*600000)"], 2)
    assert caught.value.code == "dms_details_invalid_response"


def test_exited_cli_cannot_leave_a_plugin_holding_output_pipes(tmp_path):
    pid_file = tmp_path / "child-pid"
    program = (
        "import os,pathlib,sys,time\n"
        "if os.fork() == 0:\n"
        " pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        " time.sleep(30)\n"
        "os._exit(0)\n"
    )
    try:
        with pytest.raises(AppError) as caught:
            _run_bounded([sys.executable, "-c", program, str(pid_file)], 1)
        assert caught.value.code == "dms_details_timeout"
        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            # Linux containers may retain a killed orphan until PID 1 reaps it.
            status = Path(f"/proc/{child_pid}/stat")
            if sys.platform.startswith("linux") and status.exists():
                if status.read_text().split(")", 1)[1].strip().startswith("Z"):
                    break
            assert time.monotonic() < deadline, "CLI descendant remained alive after cleanup"
            time.sleep(0.01)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def config(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 64)
    return SimpleNamespace(
        dms_details_enabled=True, dms_details_url="http://127.0.0.1:8182",
        dms_details_token_file=str(token), dms_details_timeout_seconds=60,
    )


def test_client_sends_only_order_id_and_validates_response(tmp_path):
    def handle(request):
        assert request.headers["authorization"] == "Bearer " + "a" * 64
        assert json.loads(request.content) == {"order_id": "123"}
        return httpx.Response(200, json=snapshot())

    client = DmsDetailClient(config(tmp_path), transport=httpx.MockTransport(handle))
    assert client.fetch("123") == snapshot()
    client.close()


@pytest.mark.parametrize("url", ["https://evil.invalid", "http://localhost@evil.invalid", "http://localhost/?redirect=x"])
def test_client_rejects_nonlocal_or_ambiguous_broker_urls(tmp_path, url):
    settings = config(tmp_path)
    settings.dms_details_url = url
    client = DmsDetailClient(settings, transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP")))
    with pytest.raises(AppError) as caught:
        client.fetch("123")
    assert caught.value.code == "dms_details_configuration"
    client.close()


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://evil.invalid"}),
    httpx.Response(403, text="private credentials"),
    httpx.Response(200, json=snapshot(order_id="999")),
    httpx.Response(200, json=snapshot(complete=True, errors=["truncated"])),
    httpx.Response(200, content=b"x" * 600000),
])
def test_client_errors_and_identity_mismatch_never_return_a_snapshot(tmp_path, response):
    client = DmsDetailClient(config(tmp_path), transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(AppError) as caught:
        client.fetch("123")
    assert "private" not in str(caught.value)
    client.close()


def test_broker_requires_token_and_only_accepts_an_order_id(tmp_path):
    settings = config(tmp_path)
    calls = []
    reader = SimpleNamespace(binary="fake", fetch=lambda identifier: calls.append(identifier) or snapshot())
    app = create_dms_host_app(DmsHostConfig(token_file=tmp_path / "token"), reader=reader)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["readiness_scope"] == "local_runtime"
        assert client.post("/details", json={"order_id": "123"}).status_code == 401
        headers = {"authorization": "Bearer " + "a" * 64}
        assert client.post("/details", headers=headers, json={"order_id": "123", "action": "approve-order"}).status_code == 422
        assert client.post("/details", headers=headers, json={"order_id": "123;rm"}).status_code == 422
        response = client.post("/details", headers=headers, json={"order_id": "123"})
        assert response.status_code == 200
        assert validate_snapshot(response.json(), "123") == snapshot()
    assert calls == ["123"]
    assert settings.dms_details_enabled


@pytest.mark.parametrize("suffix,verified", [
    ("?orderId=123", True),
    ("?ticketId=123", True),
    ("?ORDERID=123", True),
    ("#/ticket?orderId=123", True),
    ("#orderId=123", True),
    ("?orderId=123&ticketId=123", True),
    ("?taskId=123", False),
    ("", False),
])
def test_order_reference_requires_explicit_order_identity(suffix, verified):
    raw = {"body": {"content": json.dumps({"card_link": {"url": "https://dms.aliyun.com/" + suffix}})}}
    assert order_reference(raw, {"task_id": "123"}) == ("123", verified)


@pytest.mark.parametrize("suffix", [
    "?orderId=999", "?ticketId=999", "?orderId=123&ticketId=999",
    "?orderId=123&orderId=999", "?orderId=123%3Bcommand", "?orderId=123#/ticket?orderId=999",
])
def test_order_reference_rejects_link_and_title_or_fragment_conflicts(suffix):
    raw = {"body": {"content": {"card_link": {"url": "https://dms.aliyun.com/" + suffix}}}}
    with pytest.raises(AppError) as caught:
        order_reference(raw, {"task_id": "123"})
    assert caught.value.code == "dms_order_identity_mismatch"
