"""Exercise the official ve/TLS credential providers and record safe health metadata.

No token files are read or edited here. ve decides when its Console Login can
refresh; an expired/revoked refresh grant still requires interactive consent.
"""

import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".data" / "ve-session"
INTERVAL_SECONDS = 1800
ONLINE_CHECKS = {"credentials_present", "online_describe_projects"}


def timestamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


def valid_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def failure_status(text):
    """Classify in memory; never persist a raw CLI response or stderr."""
    text = text.lower()
    if any(phrase in text for phrase in (
        "refresh_token is invalid", "invalid_grant", "refresh token expired",
        "refresh token has expired", "session expired", "session has expired",
        "expiredtoken", "invalidsecuritytoken", "invalidtoken", "tokenexpired",
        "reauth_required", "console login expired", "no active session", "no refresh token available",
    )):
        return "reauth_required"
    if re.search(r"\bstatus(?:_code)?[=\s:\"]+401\b", text):
        return "reauth_required"
    if any(phrase in text for phrase in (
        "accessdenied", "access denied", "forbidden", "not authorized", "unauthorizedoperation",
    )) or re.search(r"\bstatus(?:_code)?[=\s:\"]+403\b", text):
        return "permission_denied"
    if any(phrase in text for phrase in (
        "timeout", "timed out", "no such host", "connection refused", "connection reset",
        "network is unreachable", "temporary failure", "tls handshake", "eof",
    )):
        return "network_error"
    if any(phrase in text for phrase in (
        "credentials not found", "missing credentials", "no credentials", "not logged in",
        "credentialsmissing", "credentialsexpired", "authentication required",
    )):
        return "reauth_required"
    return "check_failed"


class SessionProbe:
    def __init__(self, binary="ve", runner=None):
        self.binary = binary
        self.runner = runner or subprocess.run

    def command(self, arguments, *, skill="volcengine-cli", timeout=60):
        # Preserve the CLI's default profile resolution, including existing
        # environment settings. Never select another profile or copy secrets.
        env = dict(os.environ, VE_CALLER_TYPE="ai_agent", VE_CALLER_NAME="codex",
                   VE_SKILL_NAME=skill)
        try:
            result = self.runner([self.binary, *arguments], env=env, capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "network_error"
        except OSError:
            return None, "cli_unavailable"
        return result, None

    def run(self):
        result, error = self.command(["version"], timeout=20)
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\s*", result.stdout.strip()) if result else None
        if error or not match or result.returncode or tuple(map(int, match.groups())) < (1, 1, 11):
            failed = {"status": error or "cli_version_unsupported"}
            return {"ve": failed, "tls": dict(failed)}
        version = ".".join(match.groups())
        # The native call enters ve's supported automatic refresh path. It
        # prints no account identifiers or credentials even when successful.
        result, error = self.command(["sts", "GetCallerIdentity", "--output", "off"])
        native = {"status": error or ("healthy" if result.returncode == 0
                                      else failure_status(result.stderr + result.stdout))}
        # The child CLI has its own provider/cache. A native ve failure is not
        # proof TLS is unavailable, so always verify TLS independently.
        result, error = self.command(
            ["volclog", "--region", "cn-shanghai", "doctor", "--online"],
            skill="volcengine-tls",
        )
        tls = {"status": error} if error else tls_result(result)
        return {"cli_version": version, "ve": native, "tls": tls}


def tls_result(result):
    try:
        body = json.loads(result.stdout)
    except (ValueError, TypeError):
        # The wrapper's stderr may describe *ve* refresh failure while the
        # child uses a different session. Never attribute that warning to TLS.
        return {"status": "check_failed"}
    if not isinstance(body, dict):
        return {"status": "check_failed"}
    credentials = body.get("credentials")
    checks = body.get("checks")
    if not isinstance(credentials, dict) or not isinstance(checks, list):
        return {"status": "check_failed"}
    checks = [item for item in checks if isinstance(item, dict)]
    successes = {item.get("name") for item in checks if item.get("ok") is True
                 and isinstance(item.get("name"), str)}
    healthy = (result.returncode == 0 and credentials.get("present") is True
               and ONLINE_CHECKS <= successes and all(item.get("ok") is True for item in checks))
    status = "healthy" if healthy else failure_status(json.dumps(checks))
    if not healthy and credentials.get("present") is False and status == "check_failed":
        status = "reauth_required"
    safe = {"status": status}
    # Doctor may contain AK/SK/token data. Only these validated metadata fields
    # leave this function; never serialize or print the doctor's full output.
    for field in ("mode", "provider"):
        value = credentials.get(field)
        if value in ("console-login", "aksk", "sts", "sso", "static", "env", "profile"):
            safe[field] = value
    expiry = valid_timestamp(credentials.get("expires_at"))
    if expiry:
        safe["credentials_expires_at"] = expiry
    if type(credentials.get("refresh_required")) is bool:
        safe["refresh_required"] = credentials["refresh_required"]
    return safe


def save_state(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def maintain(*, state_dir=STATE_DIR, probe=None):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "job.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        path = directory / "last-run.json"
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        started = timestamp()
        result = (probe or SessionProbe()).run()
        finished = timestamp()
        healthy = all(result[name]["status"] == "healthy" for name in ("ve", "tls"))
        state = {
            "started_at": started, "finished_at": finished,
            "status": "healthy" if healthy else "attention_required",
            "reauth_required": any(result[name]["status"] == "reauth_required"
                                   for name in ("ve", "tls")),
            "interval_seconds": INTERVAL_SECONDS, **result,
        }
        for name in ("ve", "tls"):
            key = f"last_{name}_success_at"
            last_success = valid_timestamp(previous.get(key))
            if result[name]["status"] == "healthy":
                state[key] = finished
            elif last_success:
                state[key] = last_success
        save_state(path, state)
        return state


def main():
    result = maintain()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"healthy", "already_running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
