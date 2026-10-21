import fcntl
import json
import stat
import subprocess
from types import SimpleNamespace

import pytest

from scripts.maintain_ve_session import SessionProbe, failure_status, maintain, tls_result


def response(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def doctor(*, healthy=True, **changes):
    return response(json.dumps({
        "credentials": {"mode": "console-login", "provider": "console-login",
                        "present": True, "expires_at": "2026-10-21T12:00:00Z",
                        "refresh_required": False, "ak": "secret-ak", "sk": "secret-sk",
                        "token": "secret-token"},
        "checks": [{"name": "credentials_present", "ok": True},
                   {"name": "online_describe_projects", "ok": healthy,
                    "message": "ok" if healthy else "connection timed out"}],
        **changes,
    }), stderr="Warning: failed to refresh ve: refresh_token is invalid", code=0 if healthy else 1)


def test_native_session_expiry_does_not_skip_or_invalidate_tls():
    calls = []
    replies = iter([response("1.2.0"), response(stderr="refresh_token is invalid", code=1), doctor()])

    def run(command, **kwargs):
        calls.append(command)
        assert "--profile" not in command
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["VE_CALLER_TYPE"] == "ai_agent"
        assert kwargs["env"]["VE_CALLER_NAME"] == "codex"
        assert kwargs["env"]["VE_SKILL_NAME"] == (
            "volcengine-tls" if "volclog" in command else "volcengine-cli")
        return next(replies)

    result = SessionProbe(runner=run).run()
    assert calls == [["ve", "version"], ["ve", "sts", "GetCallerIdentity", "--output", "off"],
                     ["ve", "volclog", "--region", "cn-shanghai", "doctor", "--online"]]
    assert result["ve"]["status"] == "reauth_required"
    assert result["tls"]["status"] == "healthy"
    assert "secret" not in json.dumps(result)


def test_tls_network_failure_is_not_misclassified_by_native_refresh_warning():
    assert tls_result(doctor(healthy=False))["status"] == "network_error"


@pytest.mark.parametrize("message,status", [
    ("refresh_token is invalid", "reauth_required"),
    ("invalid_grant", "reauth_required"),
    ("ExpiredToken", "reauth_required"),
    ("no active session", "reauth_required"),
    ("no refresh token available", "reauth_required"),
    ("failed to refresh session token: request timed out", "network_error"),
    ("AccessDenied", "permission_denied"),
    ("credentials not found", "reauth_required"),
    ("unexpected response secret=opaque", "check_failed"),
])
def test_failures_are_classified_without_returning_response(message, status):
    assert failure_status(message) == status


def test_tls_requires_online_check_not_just_exit_zero():
    for value in [doctor(checks=[]), response("{}"), response("[]"), response("not json")]:
        assert tls_result(value)["status"] == "check_failed"


def test_tls_missing_credentials_and_refused_grant_need_user_intervention():
    assert tls_result(doctor(credentials={"present": False}))["status"] == "reauth_required"
    assert tls_result(doctor(checks=[{"name": "online_describe_projects", "ok": False,
                                     "message": "refresh_token is invalid"}]))["status"] == "reauth_required"


@pytest.mark.parametrize("detail,status", [
    ("kind=reauth_required description=console login expired; run: volclog login", "reauth_required"),
    ("status=401", "reauth_required"),
    ("status=403", "permission_denied"),
])
def test_real_doctor_failure_details(detail, status):
    data = doctor(checks=[{"name": "credentials_present", "ok": True},
                          {"name": "online_describe_projects", "ok": False, "detail": detail}])
    assert tls_result(data)["status"] == status


def test_doctor_never_forwards_unvalidated_auth_metadata():
    data = tls_result(doctor(credentials={"present": True, "provider": "secret-provider",
                                         "mode": "secret-mode", "expires_at": "secret-expiry",
                                         "refresh_required": "secret-flag", "token": "secret-token"}))
    assert data == {"status": "healthy"}


@pytest.mark.parametrize("version", ["1.1.10", "", "garbage", "1.2.0-rc1"])
def test_unsupported_versions_do_not_start_api_calls(version):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return response(version)

    assert SessionProbe(runner=run).run()["ve"]["status"] == "cli_version_unsupported"
    assert calls == [["ve", "version"]]


def test_native_timeout_still_probes_tls():
    def run(command, **kwargs):
        if command[1] == "version":
            return response("1.2.0")
        if command[1] == "sts":
            raise subprocess.TimeoutExpired(command, 60, stderr="secret")
        return doctor()

    result = SessionProbe(runner=run).run()
    assert result["ve"]["status"] == "network_error"
    assert result["tls"]["status"] == "healthy"
    assert "secret" not in json.dumps(result)


def test_state_preserves_last_success_and_saves_no_credentials(tmp_path):
    probe = SimpleNamespace(run=lambda: {"ve": {"status": "healthy"}, "tls": {"status": "healthy"}})
    first = maintain(state_dir=tmp_path, probe=probe)
    assert first["status"] == "healthy"
    assert first["reauth_required"] is False
    probe.run = lambda: {"ve": {"status": "reauth_required"}, "tls": tls_result(doctor())}
    second = maintain(state_dir=tmp_path, probe=probe)
    assert second["status"] == "attention_required"
    assert second["reauth_required"] is True
    assert second["last_ve_success_at"] == first["last_ve_success_at"]
    text = (tmp_path / "last-run.json").read_text()
    assert "secret" not in text
    assert json.loads(text) == second
    assert stat.S_IMODE((tmp_path / "last-run.json").stat().st_mode) == 0o600


def test_concurrent_job_does_not_probe_or_replace_last_result(tmp_path):
    (tmp_path / "last-run.json").write_text('{"status":"healthy"}')
    with (tmp_path / "job.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = maintain(state_dir=tmp_path, probe=SimpleNamespace(run=lambda: pytest.fail("locked")))
    assert result == {"status": "already_running"}
    assert (tmp_path / "last-run.json").read_text() == '{"status":"healthy"}'
