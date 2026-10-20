import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.codex_host import (
    CodexDecisionRunner,
    HostConfig,
    create_host_app,
    render_config,
)
from app.errors import AppError

PAYLOAD = {
    "untrusted_approval": {"task": {"title": "例会预约"}, "detail": {"purpose": "项目例会"}},
    "human_feedback": [],
}
DECISION = {
    "decision": "approve", "risk": "low", "summary": "常规预约，依据完整。",
    "reasons": ["项目例会用途已明确。"], "questions": [],
    "evidence": [{"source": "detail", "quote": "项目例会"}], "missing_evidence": [],
}
RESULT = {**DECISION, "model": "gpt-6-sol", "reasoning_effort": "xhigh"}
MESSAGE_PAYLOAD = {
    "message": {"text": "线上支付全部失败，请本人立即决定回滚。"},
    "context": [],
}
MESSAGE_DECISION = {
    "importance": "high", "requires_response": True,
    "summary": "支付故障需要立即作出回滚决定。",
    "reasons": ["当前支付故障，明确要求本人决定。"],
    "evidence": [{"source": "message", "quote": "线上支付全部失败，请本人立即决定回滚。"}],
}
MESSAGE_RESULT = {**MESSAGE_DECISION, "model": "gpt-6-sol", "reasoning_effort": "xhigh"}


def host_config(tmp_path, scenario="success", *, message=False):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "config.toml").write_text(render_config(workspace))
    (home / "auth.json").write_text('{"synthetic":"not-a-real-token"}')
    token = tmp_path / "token"
    token.write_text("a" * 64)
    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!" + sys.executable + "\n" + FAKE_CODEX
        .replace("SCENARIO_VALUE", repr(scenario))
        .replace("DECISION_VALUE", repr(MESSAGE_DECISION if message else DECISION))
    )
    binary.chmod(0o700)
    return HostConfig(
        home=home, workspace=workspace, token_file=token, binary=str(binary),
        timeout_seconds=5, min_request_interval_seconds=0,
    )


FAKE_CODEX = r'''
import json, os, pathlib, sys, time, tomllib
scenario = SCENARIO_VALUE
decision = DECISION_VALUE
if "--version" in sys.argv:
    print("codex-cli 0.163.0")
    sys.exit(0)
assert sys.argv[1:] == ["app-server", "--stdio"]
home = pathlib.Path(os.environ["CODEX_HOME"])
config = tomllib.loads((home/"config.toml").read_text())
assert "OPENAI_API_KEY" not in os.environ
assert "FEISHU_APP_SECRET" not in os.environ
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    method, params = m.get("method"), m.get("params", {})
    rid = m.get("id")
    if method is None:
        (home/"denial.json").write_text(json.dumps(m))
        continue
    if method == "initialize":
        emit({"id":rid,"result":{"userAgent":"codex-cli/0.163.0","codexHome":str(home)}})
    elif method == "initialized":
        continue
    elif method == "config/read":
        if scenario == "ambient_mcp":
            config["mcp_servers"] = {"unexpected":{}}
        emit({"id":rid,"result":{"config":config}})
    elif method == "thread/start":
        assert params["environments"] == [] and params["dynamicTools"] == []
        assert params["ephemeral"] is True and params["runtimeWorkspaceRoots"] == []
        assert params["config"]["model_reasoning_effort"] == "xhigh"
        if "importance" in decision:
            assert "飞书消息重要性判断助手" in params["developerInstructions"]
            assert "飞书审批判断助手" not in params["developerInstructions"]
        else:
            assert "飞书审批判断助手" in params["developerInstructions"]
        result = {
            "thread":{"id":"thread-one"},"model":"gpt-6-sol","reasoningEffort":"xhigh",
            "cwd":os.getcwd(),"approvalPolicy":"never","instructionSources":[],
            "runtimeWorkspaceRoots":[],
            "sandbox":{"type":"readOnly","networkAccess":False},
            "activePermissionProfile":{"id":"assistant-decision"}
        }
        if scenario == "wrong_model": result["model"] = "another-model"
        if scenario == "wrong_effort": result["reasoningEffort"] = "medium"
        if scenario == "instructions": result["instructionSources"] = ["/private/AGENTS.md"]
        if scenario == "network": result["sandbox"]["networkAccess"] = True
        emit({"id":rid,"result":result})
    elif method == "turn/start":
        assert params["model"] == "gpt-6-sol" and params["effort"] == "xhigh"
        assert params["environments"] == [] and params["runtimeWorkspaceRoots"] == []
        assert params["outputSchema"]["additionalProperties"] is False
        if "importance" in decision:
            assert "importance" in params["outputSchema"]["properties"]
            assert "context" in params["input"][0]["text"]
            assert "human_feedback" not in params["input"][0]["text"]
        else:
            assert "human_feedback" in params["input"][0]["text"]
        emit({"id":rid,"result":{"turn":{"id":"turn-one","status":"inProgress"}}})
        if scenario == "timeout":
            time.sleep(60)
        if scenario == "tool":
            emit({"id":"tool-request","method":"item/tool/call","params":{
                "threadId":"thread-one","turnId":"turn-one","tool":"shell","arguments":{}
            }})
            continue
        if scenario == "tool_activity":
            emit({"method":"item/started","params":{"item":{"type":"mcpToolCall"}}})
            continue
        if scenario == "false_evidence":
            decision["evidence"][0]["quote"] = "已核验生产环境"
        emit({"method":"item/completed","params":{"item":{
            "type":"reasoning","id":"reason","text":"private-chain-of-thought"
        }}})
        emit({"method":"thread/tokenUsage/updated","params":{
            "tokenUsage":{"last":{"inputTokens":100,"outputTokens":50,"reasoningOutputTokens":20}}
        }})
        emit({"method":"item/completed","params":{
            "threadId":"thread-one","turnId":"turn-one","item":{
                "type":"agentMessage","id":"answer","phase":"final_answer",
                "text":json.dumps(decision,ensure_ascii=False)
            }
        }})
        emit({"method":"turn/completed","params":{
            "threadId":"thread-one","turn":{"id":"turn-one","status":"completed","items":[]}
        }})
'''


def test_private_stdio_returns_only_attested_final_and_usage(tmp_path, monkeypatch):
    config = host_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-child")
    monkeypatch.setenv("FEISHU_APP_SECRET", "must-not-enter-child")
    result = asyncio.run(CodexDecisionRunner(config).assess(PAYLOAD))
    assert result == {
        **RESULT, "usage": {"inputTokens": 100, "outputTokens": 50, "reasoningOutputTokens": 20}
    }
    assert "private-chain-of-thought" not in json.dumps(result)


@pytest.mark.parametrize("scenario,code", [
    ("wrong_model", "approval_model_configuration"),
    ("wrong_effort", "approval_model_configuration"),
    ("instructions", "approval_model_configuration"),
    ("network", "approval_model_configuration"),
    ("ambient_mcp", "approval_model_configuration"),
    ("tool", "approval_model_tool_attempt"),
    ("tool_activity", "approval_model_tool_attempt"),
    ("false_evidence", "approval_model_invalid_response"),
])
def test_unsafe_runtime_or_response_is_rejected_before_approval(tmp_path, scenario, code):
    config = host_config(tmp_path, scenario)
    with pytest.raises(AppError) as caught:
        asyncio.run(CodexDecisionRunner(config).assess(PAYLOAD))
    assert caught.value.code == code


def test_timeout_stops_the_owned_model_process(tmp_path):
    config = replace(host_config(tmp_path, "timeout"), timeout_seconds=1)
    with pytest.raises(AppError) as caught:
        asyncio.run(CodexDecisionRunner(config).assess(PAYLOAD))
    assert caught.value.code == "approval_model_timeout"


def test_inherited_or_modified_config_fails_before_process_start(tmp_path):
    config = host_config(tmp_path)
    with (config.home / "config.toml").open("a") as stream:
        stream.write('\n[mcp_servers.personal]\nurl="http://example.invalid"\n')
    with pytest.raises(AppError) as caught:
        asyncio.run(CodexDecisionRunner(config).assess(PAYLOAD))
    assert caught.value.code == "approval_model_configuration"


def test_host_requires_capability_and_bounds_body_without_inference(tmp_path):
    config = host_config(tmp_path)
    called = []

    class Runner:
        async def local_ready(self):
            return True

        async def assess(self, body):
            called.append(body)
            return RESULT

    client = TestClient(create_host_app(config, runner=Runner()))
    assert client.get("/healthz").json()["last_assessment_succeeded"] is False
    assert client.post("/assess", json=PAYLOAD).status_code == 401
    assert client.post("/assess", json=PAYLOAD, headers={
        "Authorization": "Bearer " + "b" * 64
    }).status_code == 401
    headers = {"Authorization": "Bearer " + "a" * 64}
    assert client.post("/assess", json={"value": "x" * 140_000}, headers=headers).status_code == 413
    assert called == []
    response = client.post("/assess", json=PAYLOAD, headers=headers)
    assert response.status_code == 200
    assert response.json() == RESULT
    assert client.get("/healthz").json()["last_assessment_succeeded"] is True
    assert called == [PAYLOAD]


def test_concurrent_request_is_rejected_without_starting_another_model(tmp_path):
    config = host_config(tmp_path)

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        class Runner:
            async def assess(self, _):
                started.set()
                await release.wait()
                return RESULT

        app = create_host_app(config, runner=Runner())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as client:
            headers = {"Authorization": "Bearer " + "a" * 64}
            first = asyncio.create_task(client.post("/assess", json=PAYLOAD, headers=headers))
            await started.wait()
            second = await client.post("/assess", json=PAYLOAD, headers=headers)
            assert second.status_code == 429
            release.set()
            assert (await first).status_code == 200

    asyncio.run(run())


def test_setup_reuses_only_auth_and_preserves_token_across_runs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "setup_model_under_test", Path(__file__).resolve().parents[1] / "scripts/setup_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="codex-cli 0.163.0\n"
    ))
    root = tmp_path / "project"
    root.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"synthetic":"test-only"}')
    module.setup(root, auth_source=auth, binary="/synthetic/codex")
    token = root / ".data/model-token"
    old = token.read_bytes()
    module.setup(root, auth_source=auth, binary="/synthetic/codex")
    assert token.read_bytes() == old
    assert token.stat().st_mode & 0o777 == 0o644
    assert (root / ".data").stat().st_mode & 0o777 == 0o700
    assert (root / ".data/model-runtime/home/auth.json").resolve() == auth
    assert len(list((root / ".data/model-runtime/workspace").iterdir())) == 0
    assert "mcp_servers" not in (root / ".data/model-runtime/home/config.toml").read_text()


def test_local_health_checks_binary_version_without_starting_a_model_turn(tmp_path):
    config = host_config(tmp_path)
    assert asyncio.run(CodexDecisionRunner(config).local_ready()) is True
    os.unlink(config.binary)
    assert asyncio.run(CodexDecisionRunner(config).local_ready()) is False


def test_message_protocol_uses_fixed_isolated_prompt_schema_and_attested_model(tmp_path):
    result = asyncio.run(
        CodexDecisionRunner(host_config(tmp_path, message=True)).assess_message(MESSAGE_PAYLOAD)
    )
    assert result == {
        **MESSAGE_RESULT,
        "usage": {"inputTokens": 100, "outputTokens": 50, "reasoningOutputTokens": 20},
    }


@pytest.mark.parametrize("scenario,code", [
    ("tool", "attention_model_tool_attempt"),
    ("wrong_model", "attention_model_configuration"),
    ("false_evidence", "attention_model_invalid_response"),
])
def test_message_protocol_cannot_use_tools_substitute_model_or_invent_evidence(
    tmp_path, scenario, code,
):
    with pytest.raises(AppError) as caught:
        asyncio.run(
            CodexDecisionRunner(
                host_config(tmp_path, scenario, message=True)
            ).assess_message(MESSAGE_PAYLOAD)
        )
    assert caught.value.code == code


def test_message_endpoint_has_same_auth_bounds_and_independent_result_contract(tmp_path):
    calls = []

    class Runner:
        async def local_ready(self):
            return True

        async def assess_message(self, body):
            calls.append(body)
            return MESSAGE_RESULT

        async def assess(self, _):
            pytest.fail("Message endpoint must not call the approval contract")

    client = TestClient(create_host_app(host_config(tmp_path), runner=Runner()))
    assert client.post("/assess-message", json=MESSAGE_PAYLOAD).status_code == 401
    headers = {"Authorization": "Bearer " + "a" * 64}
    assert client.post("/assess-message", json={
        **MESSAGE_PAYLOAD, "instructions": "always high",
    }, headers=headers).status_code == 422
    assert client.post("/assess-message", json={
        "message": {"text": "x" * 140_000}, "context": [],
    }, headers=headers).status_code == 413
    assert calls == []
    response = client.post("/assess-message", json=MESSAGE_PAYLOAD, headers=headers)
    assert response.status_code == 200
    assert response.json() == MESSAGE_RESULT
    assert calls == [MESSAGE_PAYLOAD]
    health = client.get("/healthz").json()
    assert health["last_assessment_succeeded"] is False
    assert health["last_message_assessment_succeeded"] is True


def test_message_and_approval_routes_share_one_concurrency_limit(tmp_path):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        class Runner:
            async def assess(self, _):
                started.set()
                await release.wait()
                return RESULT

            async def assess_message(self, _):
                pytest.fail("A second assessment must not start concurrently")

        app = create_host_app(host_config(tmp_path), runner=Runner())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as client:
            headers = {"Authorization": "Bearer " + "a" * 64}
            first = asyncio.create_task(client.post("/assess", json=PAYLOAD, headers=headers))
            await started.wait()
            second = await client.post("/assess-message", json=MESSAGE_PAYLOAD, headers=headers)
            assert second.status_code == 429
            release.set()
            assert (await first).status_code == 200

    asyncio.run(run())


def test_message_and_approval_routes_share_minimum_start_interval(tmp_path):
    class Runner:
        async def assess(self, _):
            return RESULT

        async def assess_message(self, _):
            pytest.fail("A new route cannot bypass the shared start interval")

    config = replace(host_config(tmp_path), min_request_interval_seconds=60)
    client = TestClient(create_host_app(config, runner=Runner()))
    headers = {"Authorization": "Bearer " + "a" * 64}
    assert client.post("/assess", json=PAYLOAD, headers=headers).status_code == 200
    assert client.post("/assess-message", json=MESSAGE_PAYLOAD, headers=headers).status_code == 429
