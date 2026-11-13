"""Bounded, read-only DMS OpenAPI access through the host's Alibaba Cloud CLI.

The CLI owns OAuth refresh. Credentials and arbitrary CLI commands are never
accepted from the application or included in a detail snapshot.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime

from app.errors import AppError

MAX_DETAIL_BYTES = 512 * 1024
MAX_CLI_ERROR_BYTES = 8192
PAGE_SIZE = 100
MAX_PAGES = 10
READ_ACTIONS = {
    "get-order-base-info": "OrderBaseInfo",
    "get-data-correct-order-detail": "DataCorrectOrderDetail",
    "get-perm-apply-order-detail": "PermApplyOrderDetail",
    "get-data-export-order-detail": "DataExportOrderDetail",
    "list-data-correct-pre-check-sql": "PreCheckSQLList",
}
DETAIL_ACTIONS = {
    "DC_COMMON": "get-data-correct-order-detail",
    "DC_CHUNK": "get-data-correct-order-detail",
    "DC_CRON_CLEAR": "get-data-correct-order-detail",
    "DC_PROC": "get-data-correct-order-detail",
    "DC_BIG_FILE": "get-data-correct-order-detail",
    "PERM_APPLY": "get-perm-apply-order-detail",
    "DATA_EXPORT": "get-data-export-order-detail",
}


def dms_error(code="dms_details_unavailable", status=502):
    messages = {
        "dms_details_configuration": "DMS 详情服务未配置，请检查本机服务和阿里云 CLI 登录。",
        "dms_details_unavailable": "暂时无法读取 DMS 详情，请检查阿里云登录、权限和网络。",
        "dms_details_timeout": "读取 DMS 详情超时，稍后重新核验。",
        "dms_details_invalid_response": "DMS 详情不完整或工单身份不匹配，需要重新核验。",
        "dms_details_invalid_order": "DMS 工单编号无效，未查询详情。",
        "dms_details_busy": "DMS 详情服务忙，稍后重新核验。",
        "dms_details_disabled": "DMS 详情读取已暂停。",
    }
    return AppError(code, messages.get(code, messages["dms_details_unavailable"]), status)


def order_id(value):
    if type(value) is int:
        value = str(value)
    if (
        not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
        or int(value) > 2**63 - 1
    ):
        raise dms_error("dms_details_invalid_order", 422)
    return value


def _valid_revision(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value,
    ):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def validate_snapshot(value, expected_order):
    """Validate the broker contract before caching or assessing any material."""
    try:
        expected_order = order_id(expected_order)
        if (
            not isinstance(value, dict)
            or value.get("order_id") != expected_order
            or not isinstance(value.get("base"), dict)
            or order_id(value["base"].get("OrderId")) != expected_order
            or not isinstance(value.get("detail"), dict)
            or not isinstance(value.get("sql_checks"), list)
            or any(not isinstance(item, dict) for item in value["sql_checks"])
            or type(value.get("complete")) is not bool
            or not isinstance(value.get("errors"), list)
            or len(value["errors"]) > 20
            or any(not isinstance(item, str) or len(item) > 500 for item in value["errors"])
            or (value["complete"] and (value["errors"] or not value["detail"]))
            or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_DETAIL_BYTES
        ):
            raise ValueError("Invalid snapshot")
        return value
    except (ValueError, TypeError, RecursionError, AppError):
        raise dms_error("dms_details_invalid_response") from None


def _run_bounded(argv, timeout):
    """No shell, stdin, unbounded output, retry, or upstream error disclosure."""
    process = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        output = {"stdout": bytearray(), "stderr": bytearray()}
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise dms_error("dms_details_timeout", 504)
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = output[key.data]
                    target.extend(chunk)
                    limit = MAX_DETAIL_BYTES if key.data == "stdout" else MAX_CLI_ERROR_BYTES
                    if len(target) > limit:
                        raise dms_error("dms_details_invalid_response")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise dms_error("dms_details_timeout", 504)
            if process.wait(timeout=remaining):
                raise dms_error()
        return bytes(output["stdout"])
    except subprocess.TimeoutExpired:
        raise dms_error("dms_details_timeout", 504) from None
    except OSError:
        raise dms_error("dms_details_configuration", 503) from None
    finally:
        if process is not None:
            # The CLI may have exited while a plugin descendant still owns its
            # stdout/stderr pipe. Always clean up the process group we created.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()


class DmsCliReader:
    def __init__(self, *, binary=None, profile="", tenant_id="", timeout=50, run=None):
        self.binary = binary or shutil.which("aliyun") or ""
        if profile and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", profile):
            raise dms_error("dms_details_configuration", 503)
        self.profile = profile
        self.tenant_id = order_id(tenant_id) if tenant_id else ""
        self.timeout = timeout
        self.run = run or _run_bounded

    def _call(self, action, identifier, deadline, *, page=None):
        if action not in READ_ACTIONS or not self.binary:
            raise dms_error("dms_details_configuration", 503)
        argv = [self.binary]
        if self.profile:
            argv.extend(["--profile", self.profile])
        argv.extend(["dms-enterprise", action, "--order-id", order_id(identifier)])
        if self.tenant_id:
            argv.extend(["--tid", self.tenant_id])
        if page is not None:
            if action != "list-data-correct-pre-check-sql" or not 1 <= page <= MAX_PAGES:
                raise dms_error("dms_details_configuration", 503)
            argv.extend(["--page-number", str(page), "--page-size", str(PAGE_SIZE)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise dms_error("dms_details_timeout", 504)
        raw = self.run(argv, remaining)
        try:
            if len(raw) > MAX_DETAIL_BYTES:
                raise ValueError("Response too large")
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("Success") is not True:
                raise dms_error()
            return response
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise dms_error("dms_details_invalid_response") from None

    def fetch(self, identifier):
        identifier = order_id(identifier)
        deadline = time.monotonic() + self.timeout
        base = self._call("get-order-base-info", identifier, deadline).get("OrderBaseInfo")
        if not isinstance(base, dict) or order_id(base.get("OrderId")) != identifier:
            raise dms_error("dms_details_invalid_response")
        snapshot = {
            "order_id": identifier, "base": base, "detail": {}, "sql_checks": [],
            "complete": False, "errors": [], "fetched_at": datetime.now(UTC).isoformat(),
        }
        action = DETAIL_ACTIONS.get(base.get("PluginType"))
        if not action:
            snapshot["errors"].append("dms_details_unsupported_type")
            return validate_snapshot(snapshot, identifier)
        try:
            response = self._call(action, identifier, deadline)
            detail = response.get(READ_ACTIONS[action])
            if not isinstance(detail, dict) or not detail:
                raise dms_error("dms_details_invalid_response")
            snapshot["detail"] = detail
            if action == "get-data-correct-order-detail":
                seen = set()
                for page in range(1, MAX_PAGES + 1):
                    result = self._call(
                        "list-data-correct-pre-check-sql", identifier, deadline, page=page,
                    ).get("PreCheckSQLList")
                    if (
                        not isinstance(result, list) or len(result) > PAGE_SIZE
                        or any(not isinstance(row, dict) for row in result)
                    ):
                        raise dms_error("dms_details_invalid_response")
                    fingerprint = json.dumps(result, sort_keys=True, ensure_ascii=False)
                    if result and fingerprint in seen:
                        snapshot["errors"].append("dms_details_repeated_page")
                        break
                    seen.add(fingerprint)
                    snapshot["sql_checks"].extend(result)
                    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_DETAIL_BYTES:
                        snapshot["sql_checks"] = snapshot["sql_checks"][:-len(result)]
                        snapshot["errors"].append("dms_details_truncated")
                        break
                    if len(result) < PAGE_SIZE:
                        break
                else:
                    snapshot["errors"].append("dms_details_truncated")
                order = detail.get("OrderDetail")
                if isinstance(order, dict) and str(order.get("SqlType", "")).upper() == "ATTACHMENT":
                    snapshot["errors"].append("dms_sql_attachment_not_reviewed")
        except AppError as exc:
            snapshot["errors"].append(exc.code)
        if not snapshot["errors"]:
            # Detail and SQL pagination span several calls. A changed revision is
            # incomplete evidence; an absent modification time never proves a
            # stable revision. The risk layer still checks the SQL bodies match.
            try:
                current = self._call("get-order-base-info", identifier, deadline).get("OrderBaseInfo")
            except AppError as exc:
                snapshot["errors"].append(exc.code)
            else:
                if not isinstance(current, dict) or order_id(current.get("OrderId")) != identifier:
                    raise dms_error("dms_details_invalid_response")
                if any(
                    current.get(key) != base.get(key)
                    for key in ("LastModifyTime", "PluginType", "StatusCode")
                ):
                    snapshot["errors"].append("dms_details_changed_during_read")
                elif not _valid_revision(base.get("LastModifyTime")):
                    snapshot["errors"].append("dms_details_revision_unverified")
        snapshot["complete"] = not snapshot["errors"]
        return validate_snapshot(snapshot, identifier)
