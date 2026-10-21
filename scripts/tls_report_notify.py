"""Send daily report results through the existing Octopus bot to its bound owner.

The host owns the durable delivery ledger. The container retains all Feishu and
DB credentials; only a safe payload and the resolved single-user identity cross
stdin/stdout. No approval, phone, arbitrary-recipient, or CLI-user send is used.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".data" / "tls-error-reports" / "notifications"
SAFE_ERRORS = {
    "SourceError": "TLS 日志查询未完成",
    "EvidenceError": "交叉核验未完成",
    "InventoryError": "服务归属清单未能更新",
    "PublishError": "飞书文档发布未确认",
    "ModelError": "模型分析未完成",
    "ModelRunError": "模型分析未完成",
    "ModelUnavailable": "模型分析未完成",
    "ValidationError": "分析结果未通过校验",
    "ValueError": "输入或分析结果未通过校验",
    "TimeoutError": "任务执行超时",
    "FileNotFoundError": "本地任务依赖不可用",
    "PermissionError": "任务访问权限不足",
}
SUCCESS = {"published", "already_published"}
SKIP = {"collected", "already_running"}

# This fixed program executes inside the already-running app container. It has
# no dependency on host scripts being copied into the app image.
BRIDGE = r'''
import hashlib
import json
import re
import sys
from app.config import Settings
from app.db import make_database
from app.errors import AppError
from app.feishu import FeishuClient
from app.models import Account
from app.security import Security
from sqlalchemy import func, select

engine = None
feishu = None
try:
    request = json.load(sys.stdin)
    if request.get("mode") not in {"prepare", "send"}:
        raise ValueError("unsupported mode")
    settings = Settings()
    engine, sessions = make_database(settings)
    with sessions() as db:
        account = db.get(Account, 1)
        if account is None or db.scalar(select(func.count()).select_from(Account)) != 1:
            raise AppError("connect_required", "", 409)
        owner = account.open_id
        if not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", owner):
            raise AppError("connect_required", "", 409)
        identity = hashlib.sha256(json.dumps([
            settings.feishu_app_id, owner, account.tenant_key,
        ]).encode()).hexdigest()
    feishu = FeishuClient(settings, Security(settings, sessions), sessions)
    if request["mode"] == "prepare":
        if not feishu.self_message_permissions()["ready"]:
            raise AppError("self_message_permission_required", "", 409)
        answer = {"ok": True, "owner": owner, "identity": identity}
    else:
        if request.get("identity") != identity or request.get("owner") != owner:
            raise AppError("status_owner_changed", "", 409)
        sent = feishu.send_self_status(
            request["text"], request["uuid"], expected_owner=request["owner"],
        )
        answer = {"ok": True, "message_id": sent["message_id"]}
except Exception as error:
    code = getattr(error, "code", "notification_unavailable")
    allowed = {"connect_required", "self_message_permission_required", "status_owner_changed",
               "not_configured", "invalid_status_message", "invalid_status_response"}
    answer = {"ok": False, "error_code": code if code in allowed else "notification_unavailable"}
finally:
    if feishu is not None:
        feishu.close()
    if engine is not None:
        engine.dispose()
print(json.dumps(answer))
'''


class NotificationError(RuntimeError):
    """A public error without command stderr, raw logs, or credentials."""


class AppBotTransport:
    def __init__(self, binary="docker"):
        self.binary = binary

    def _run(self, request):
        try:
            result = subprocess.run(
                [self.binary, "compose", "--project-directory", str(ROOT),
                 "-f", str(ROOT / "compose.yaml"), "exec", "-T", "app", "python", "-c", BRIDGE],
                input=json.dumps(request, ensure_ascii=False), capture_output=True,
                text=True, timeout=90, cwd=ROOT,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NotificationError("notification_transport_unavailable") from None
        if result.returncode:
            raise NotificationError("notification_transport_unavailable")
        try:
            response = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise NotificationError("notification_invalid_response") from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            code = response.get("error_code") if isinstance(response, dict) else None
            allowed = {"connect_required", "self_message_permission_required", "status_owner_changed",
                       "not_configured", "invalid_status_message", "invalid_status_response"}
            raise NotificationError(code if code in allowed else "notification_unavailable")
        return response

    def prepare(self):
        response = self._run({"mode": "prepare"})
        if (not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", str(response.get("owner", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(response.get("identity", "")))):
            raise NotificationError("notification_invalid_identity")
        return {"owner": response["owner"], "identity": response["identity"]}

    def send(self, text, identifier, identity):
        response = self._run({"mode": "send", "text": text, "uuid": identifier, **identity})
        message_id = response.get("message_id", "")
        if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,128}", message_id):
            raise NotificationError("notification_invalid_response")
        return {"message_id": message_id}


def report_message(report_date, result):
    day = date.fromisoformat(str(report_date)).isoformat()
    if not isinstance(result, dict):
        raise NotificationError("invalid_report_result")
    status = result.get("status")
    if not isinstance(status, str):
        raise NotificationError("invalid_report_status")
    if status in SKIP:
        return None
    if status in SUCCESS:
        errors, services = result.get("errors"), result.get("services")
        if type(errors) is not int or type(services) is not int or errors < 0 or services < 0:
            raise NotificationError("invalid_report_statistics")
        url = result.get("url")
        if not isinstance(url, str):
            raise NotificationError("invalid_report_link")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise NotificationError("invalid_report_link") from None
        if (parsed.scheme != "https" or parsed.netloc != "atrenew.feishu.cn"
                or not re.fullmatch(r"/wiki/[A-Za-z0-9]+", parsed.path)
                or parsed.query or parsed.fragment):
            raise NotificationError("invalid_report_link")
        return "success", (
            f"{day} 平台研发错误日志日报已完成\n"
            f"最近24小时：{services} 个服务，{errors:,} 条错误。\n"
            f"分类、处置与 TLS 定位查询：{url}"
        )
    if status == "failed":
        error_type = result.get("error_type")
        reason = SAFE_ERRORS.get(error_type, "任务未完成") if isinstance(error_type, str) else "任务未完成"
        return "failed", (
            f"{day} 平台研发错误日志日报未完成\n"
            f"当前状态：{reason}。\n"
            "本次报告尚未确认发布；已有日报保留。"
        )
    raise NotificationError("invalid_report_status")


def _save(path, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_error(error):
    if isinstance(error, NotificationError) and re.fullmatch(r"[a-z_]{1,64}", str(error)):
        return str(error)
    return "notification_unavailable"


def _public(state):
    return {key: state[key] for key in ("report_date", "outcome", "status", "message_id",
                                       "error_code", "submitted_at", "sent_at") if key in state}


def notify_report(report_date, result, *, state_dir=STATE_DIR, transport=None):
    """Notify once per report revision; daily failure followed by success is allowed.

    Returns a safe status dict. Preflight failures are retryable. Once a send has
    been attempted, timeout/interruption is recorded as unknown and never resent.
    ``result`` is the runner result: published/already_published with errors,
    services,url,document_id,content_hash; or failed with a safe exception class
    name in error_type. The hash is the XML report body hash, not published_at.
    """
    rendered = report_message(report_date, result)
    day = date.fromisoformat(str(report_date)).isoformat()
    if rendered is None:
        return {"report_date": day, "status": "skipped"}
    outcome, text = rendered
    if outcome == "success":
        document_id, content_hash = result.get("document_id"), result.get("content_hash")
        if (not isinstance(document_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", document_id)
                or not isinstance(content_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", content_hash)):
            raise NotificationError("invalid_report_revision")
        revision = hashlib.sha256(json.dumps([day, document_id, content_hash]).encode()).hexdigest()[:24]
        suffix = f"success-{revision}"
    else:
        suffix = "failed"
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{day}-{suffix}.json"
    transport = transport or AppBotTransport()
    with (directory / f"{day}-{suffix}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"report_date": day, "outcome": outcome, "status": "already_running"}
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            raise NotificationError("notification_state_unreadable") from None
        if not isinstance(state, dict):
            raise NotificationError("notification_state_invalid")
        if state and (state.get("report_date") != day or state.get("outcome") != outcome):
            raise NotificationError("notification_state_invalid")
        if state.get("submitted_at"):
            if state.get("status") == "sending":
                state.update(status="unknown", error_code="notification_interrupted")
                _save(path, state)
            return _public(state)
        state.update(report_date=day, outcome=outcome, text=text,
                     uuid=state.get("uuid") or str(uuid4()))
        if outcome == "success":
            state.update(document_id=document_id, content_hash=content_hash)
        try:
            identity = transport.prepare()
            if state.get("identity") and state["identity"] != identity:
                raise NotificationError("status_owner_changed")
        except Exception as error:
            state.update(status="failed", error_code=_safe_error(error))
            _save(path, state)
            return _public(state)
        state.update(identity=identity, status="sending", submitted_at=_stamp())
        state.pop("error_code", None)
        _save(path, state)
        try:
            response = transport.send(text, state["uuid"], identity)
            message_id = response.get("message_id", "")
            if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,128}", message_id):
                raise NotificationError("notification_invalid_response")
        except Exception as error:
            state.update(status="unknown", error_code=_safe_error(error))
        else:
            state.update(status="sent", message_id=message_id, sent_at=_stamp())
        _save(path, state)
        return _public(state)
