"""Bounded, read-only production evidence for the daily Shanghai TLS report.

Authentication reuses only the existing Grafana MCP server's environment settings
and cockpit's own login. Credentials and raw command output never leave clients.
Missing series are explicitly absent; neither missing data nor temporal proximity
to a release establishes the cause of a log error.
"""

import json
import math
import os
import re
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

DATASOURCE_UID = "fftmr3orskum8a"
GRAFANA_CLUSTER = "pro-jdcloud-sh"
CRANE_CLUSTER = "prod-jd-cloud"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_SERVICES = 12
RELEASE_LIMIT = 200


class EvidenceUnavailable(RuntimeError):
    """Contains only a fixed, non-sensitive failure code."""


def instant(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        result = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Evidence window timestamps must include a timezone")
    return result.astimezone(UTC)


class GrafanaEvidence:
    def __init__(self, config_path=None, *, transport=None):
        try:
            config = Path(config_path or Path.home() / ".codex/config.toml")
            values = tomllib.loads(config.read_text())["mcp_servers"]["grafana"]["env"]
            url = os.environ.get("GRAFANA_URL") or values["GRAFANA_URL"]
            token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN") or values["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
            parts = urlsplit(url)
            if (parts.scheme != "https" or not parts.hostname or parts.username
                    or parts.password or parts.query or parts.fragment or not token):
                raise ValueError("Invalid Grafana configuration")
        except (OSError, KeyError, TypeError, ValueError):
            raise EvidenceUnavailable("grafana_credentials_unavailable") from None
        self.base_url = url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url, headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(35, connect=10), transport=transport, follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def query(self, expression, end):
        try:
            response = self.client.get(
                f"/api/datasources/proxy/uid/{DATASOURCE_UID}/api/v1/query",
                params={"query": expression, "time": end.timestamp(), "timeout": "25s"},
            )
            response.raise_for_status()
            body = response.json()
            if (body.get("status") != "success" or body.get("warnings")
                    or body.get("data", {}).get("resultType") != "vector"
                    or not isinstance(body["data"].get("result"), list)):
                raise ValueError("Incomplete Prometheus response")
            return body["data"]["result"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise EvidenceUnavailable("grafana_query_failed") from None


class CraneEvidence:
    def __init__(self, binary="cockpit"):
        self.binary = binary

    def releases(self, service, start, end):
        # CLI filters on creation date, not deployment time; widen its prefilter
        # and apply exact app/cluster/deployment-time filtering to the response.
        arguments = [
            self.binary, "deploy", "release", "list", "--all-apps", "--app", service,
            "--cluster", CRANE_CLUSTER, "--begin-date",
            (start.astimezone(SHANGHAI) - timedelta(days=1)).date().isoformat(),
            "--end-date", (end.astimezone(SHANGHAI) + timedelta(days=1)).date().isoformat(),
            "--limit", str(RELEASE_LIMIT), "--no-color", "-o", "json",
        ]
        try:
            process = subprocess.run(arguments, capture_output=True, text=True, timeout=45)
            if process.returncode:
                raise ValueError("Command failed")
            rows = json.loads(process.stdout)
            if rows == {"message": "No releases found."}:
                rows = []
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Invalid release records")
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise EvidenceUnavailable("crane_release_query_failed") from None
        releases = []
        invalid_times = 0
        for row in rows:
            if row.get("AppName") != service or row.get("Cluster") != CRANE_CLUSTER:
                continue
            try:
                deployed = datetime.strptime(row["DeployedAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
            except (ValueError, KeyError, TypeError):
                invalid_times += 1
                continue
            if start <= deployed < end:
                releases.append({
                    "id": str(row.get("ID", ""))[:40], "cluster": CRANE_CLUSTER,
                    "deployed_at": deployed.isoformat(),
                    "status": str(row.get("ReleaseStatus", ""))[:40],
                    "commit": str(row.get("CommitID", ""))[:40],
                })
        return {
            "status": "partial" if len(rows) >= RELEASE_LIMIT or invalid_times else "available",
            "releases": sorted(releases, key=lambda row: row["deployed_at"]),
            "records_scanned": len(rows), "invalid_deployment_times": invalid_times,
            "coverage": "按创建日期前后扩展一天检索，最多200条；未命中不能证明没有发布。",
            "interpretation": "发布时间接近仅是关联线索，不能据此认定发布导致错误。",
        }


def queries(services, seconds):
    selector = json.dumps("|".join(re.escape(service) for service in services))
    cluster = f'cluster="{GRAFANA_CLUSTER}"'
    container = f"{cluster},container=~{selector}"
    destination = f'{cluster},reporter="destination",destination_app=~{selector}'
    trace = f"{cluster},service_name=~{selector}"
    duration = f"[{seconds}s]"
    return {
        "requests": ("destination_app", f"sum by(destination_app)(increase(istio_requests_total{{{destination}}}{duration}))"),
        "http_5xx": ("destination_app", f'sum by(destination_app)(increase(istio_requests_total{{{destination},response_code=~"5..|5xx"}}{duration}))'),
        "pod_restarts": ("container", f"sum by(container)(increase(kube_pod_container_status_restarts_total{{{container}}}{duration}))"),
        "peak_single_container_memory_bytes": ("container", f"max by(container)(max_over_time(container_memory_working_set_bytes{{{container}}}{duration}))"),
        "oom_reason_observed": ("container", f'max by(container)(max_over_time(kube_pod_container_status_last_terminated_reason{{{container},reason="OOMKilled"}}{duration}))'),
        "client_errors": ("service_name", f'sum by(service_name)(increase(traces_span_metrics_calls_total{{{trace},span_kind="SPAN_KIND_CLIENT",status_code="STATUS_CODE_ERROR"}}{duration}))'),
        "downstream_errors": ("service_name", f'topk(5,sum by(service_name,server_address)(increase(traces_span_metrics_calls_total{{{trace},span_kind="SPAN_KIND_CLIENT",status_code="STATUS_CODE_ERROR"}}{duration}))) by(service_name)'),
        "downstream_p95_ms": ("service_name", f'topk(5,histogram_quantile(.95,sum by(le,service_name,server_address)(increase(traces_span_metrics_duration_milliseconds_bucket{{{trace},span_kind="SPAN_KIND_CLIENT"}}{duration})))) by(service_name)'),
        "trace_error_types": ("service_name", f'topk(5,sum by(service_name,exception_type)(increase(traces_span_metrics_events_total{{{trace},status_code="STATUS_CODE_ERROR"}}{duration}))) by(service_name)'),
    }


def collect_evidence(service_names, start, end, *, grafana=None, crane=None):
    """Return bounded evidence; start/end accept aware datetimes, ISO, epoch s/ms.

    The caller supplies selected platform service names (normally the top eight).
    This module never decides ownership, writes operations, or derives root cause.
    """
    names = list(dict.fromkeys(service_names))
    if len(names) > MAX_SERVICES or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}", name) for name in names):
        raise ValueError("Evidence requires at most 12 valid exact service names")
    start, end = instant(start), instant(end)
    seconds = int((end - start).total_seconds())
    if not 0 < seconds <= 26 * 3600:
        raise ValueError("Evidence window must be positive and at most 26 hours")
    result = {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "grafana_cluster": GRAFANA_CLUSTER, "crane_cluster": CRANE_CLUSTER,
        "services": {name: {"metrics": {}, "releases": {}} for name in names},
        "limitations": [
            "无指标样本不等于零错误；Prometheus increase 是区间估算，Trace 指标不代表所有请求。",
            "OOM reason 仅表示窗口内观测到上次退出原因为 OOM，不证明本窗口发生了新 OOM。",
            "指标和发布用于交叉验证；时间相关性不证明根因。",
        ],
    }
    if not names:
        result["status"] = "no_services"
        return result
    owns_grafana = grafana is None
    grafana_error = None
    if grafana is None:
        try:
            grafana = GrafanaEvidence()
        except EvidenceUnavailable as error:
            grafana_error = str(error)
    definitions = queries(names, seconds)
    try:
        for metric_name, (label, expression) in definitions.items():
            try:
                if grafana is None:
                    raise EvidenceUnavailable(grafana_error)
                rows = grafana.query(expression, end)
                for name in names:
                    samples = []
                    for row in rows:
                        labels = row.get("metric", {})
                        if labels.get(label) != name:
                            continue
                        try:
                            value = float(row["value"][1])
                        except (KeyError, ValueError, TypeError, IndexError):
                            continue
                        if not math.isfinite(value):
                            continue
                        samples.append({"value": value, **{
                            key: str(labels[key])[:200] for key in ("server_address", "exception_type") if key in labels
                        }})
                    result["services"][name]["metrics"][metric_name] = {
                        "status": "available" if samples else "no_data", "samples": samples,
                    }
            except EvidenceUnavailable as error:
                for name in names:
                    result["services"][name]["metrics"][metric_name] = {"status": "unavailable", "reason": str(error)}
        if grafana is not None:
            result["grafana_dashboard"] = grafana.base_url + "/d/pro-single-service-health-map?" + urlencode({
                "from": int(start.timestamp() * 1000), "to": int(end.timestamp() * 1000),
                "var-cluster": GRAFANA_CLUSTER,
            })
    finally:
        if owns_grafana and grafana is not None:
            grafana.close()
    crane = crane or CraneEvidence()

    def release_evidence(name):
        try:
            return name, crane.releases(name, start, end)
        except EvidenceUnavailable as error:
            return name, {"status": "unavailable", "reason": str(error)}

    with ThreadPoolExecutor(max_workers=3) as executor:
        for name, data in executor.map(release_evidence, names):
            result["services"][name]["releases"] = data
    statuses = [item["status"] for service in result["services"].values()
                for item in [*service["metrics"].values(), service["releases"]]]
    result["status"] = "partial" if any(value in {"unavailable", "partial"} for value in statuses) else "available"
    return result
