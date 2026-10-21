"""Read-only TLS collection with exact service ownership and explicit coverage."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from scripts.tls_report_samples import extract_sample

REGION = "cn-shanghai"
PROJECT_NAME = "pro-jd-sh-service"
TOPIC_NAME = "error"
PAGE_SIZE = 100
# TLS SQL LIMIT supports up to 100,000 rows. Leave room above routine cardinality
# without asking the server to materialize the maximum for every service.
GROUP_LIMIT = 10000
MAX_GROUP_LIMIT = 100000
COUNT_PAGE_SIZE = 500


class SourceError(RuntimeError):
    """Safe error text: credentials and raw log bodies must never enter job logs."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class TlsClient:
    def __init__(self, binary="ve"):
        self.binary = binary
        self.env = dict(os.environ, VE_CALLER_TYPE="ai_agent", VE_CALLER_NAME="codex",
                        VE_SKILL_NAME="volcengine-tls")
        try:
            result = subprocess.run([binary, "version"], env=self.env, capture_output=True,
                                    text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise SourceError("TLS CLI 不可用。") from None
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\s*", result.stdout.strip())
        if result.returncode or not match or tuple(map(int, match.groups())) < (1, 1, 11):
            raise SourceError("TLS 查询要求 ve >= 1.1.11。")

    def call(self, action, inputs):
        # File mode avoids stdout limits and keeps raw samples in a private,
        # automatically removed directory. Authentication stays in the CLI.
        with tempfile.TemporaryDirectory(prefix="octopus-tls-") as folder:
            command = [self.binary, "volclog", "--region", REGION,
                       "--output-mode", "file", "--output-dir", folder,
                       "tool", "exec", action, "--input", json.dumps(inputs)]
            try:
                result = subprocess.run(command, env=self.env, capture_output=True,
                                        text=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired):
                raise SourceError(f"TLS 请求未完成：{action}。", retryable=True) from None
            artifacts = list(Path(folder).glob("*.json"))
            try:
                body = json.loads(artifacts[0].read_text()) if len(artifacts) == 1 else json.loads(result.stdout)
            except (OSError, ValueError, TypeError):
                raise SourceError(f"TLS 结果无法核验：{action}。") from None
            if result.returncode or body.get("status") != "success":
                error = body.get("error") or {}
                code = re.sub(r"[^A-Za-z0-9_.-]", "", str(error.get("code", "unknown")))[:80]
                retryable = (error.get("kind") in {"server", "network", "transport", "timeout"}
                             or error.get("statusCode") in {408, 429, 500, 502, 503, 504}
                             or code in {"InternalServerError", "ServiceUnavailable", "TooManyRequests",
                                         "Throttling", "QueryTimeout", "SearchTimeout", "ServerBusy"})
                raise SourceError(f"TLS 查询失败：{action}（{code}）。", retryable=retryable)
            if not isinstance(body.get("data"), dict):
                raise SourceError(f"TLS 结果缺失：{action}。")
            return body["data"]


def _catalog(client, action, inputs, key, identity):
    rows, seen, expected = [], set(), None
    for page in range(1, 1001):
        body = client.call(action, {**inputs, "PageSize": PAGE_SIZE, "PageNumber": page})
        batch, total = body.get(key), body.get("Total")
        if not isinstance(batch, list) or type(total) is not int or total < 0:
            raise SourceError("TLS 目录分页字段缺失。")
        if expected is None:
            expected = total
        elif expected != total:
            raise SourceError("TLS 目录在分页期间发生变化。")
        for row in batch:
            value = row.get(identity) if isinstance(row, dict) else None
            if not value or value in seen:
                raise SourceError("TLS 目录分页重复或标识缺失。")
            seen.add(value)
            rows.append(row)
        if len(rows) == total:
            return rows
        if len(rows) > total or len(batch) != PAGE_SIZE:
            raise SourceError("TLS 目录分页不完整。")
    raise SourceError("TLS 目录超过分页保护上限。")


def resolve_topic(client):
    projects = _catalog(client, "project.describe-projects", {
        "ProjectName": PROJECT_NAME, "IsFullName": True,
    }, "Projects", "ProjectId")
    exact = [item for item in projects if item.get("ProjectName") == PROJECT_NAME]
    if len(exact) != 1:
        raise SourceError("无法唯一确认 pro-jd-sh-service 日志项目。")
    project = exact[0]
    topics = _catalog(client, "topic.describe-topics", {
        "ProjectId": project["ProjectId"], "TopicName": TOPIC_NAME, "IsFullName": True,
    }, "Topics", "TopicId")
    exact = [item for item in topics if item.get("TopicName") == TOPIC_NAME
             and item.get("ProjectId") == project["ProjectId"]]
    if len(exact) != 1:
        raise SourceError("无法唯一确认 error 日志主题。")
    topic = exact[0]
    index = client.call("index.describe", {"TopicId": topic["TopicId"]})
    fields = {item.get("Key"): item.get("Value", {})
              for item in (index.get("KeyValue", []) + index.get("UserInnerKeyValue", []))}
    if any(not fields.get(name, {}).get("SqlFlag") for name in ("topic", "__content__")):
        raise SourceError("error 主题缺少服务归属或日志正文 SQL 索引。")
    return project, topic, index


def _seconds(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Log window requires timezone-aware datetime")
        return int(value.timestamp())
    if type(value) is int:
        return value
    raise ValueError("Log window requires Unix seconds or aware datetime")


def _rows(client, topic_id, start, end, query):
    body = client.call("log.search", {"TopicId": topic_id, "StartTime": start,
                                     "EndTime": end, "Query": query, "MustComplete": True})
    if body.get("ResultStatus") != "complete":
        # Never add counts from a partial scan. Re-query disjoint narrower windows.
        if body.get("ResultStatus") == "incomplete" and end - start > 60:
            middle = (start + end) // 2
            return (_rows(client, topic_id, start, middle, query)
                    + _rows(client, topic_id, middle, end, query))
        raise SourceError("TLS 扫描未完成，不能把部分结果当成全量。")
    analysis = body.get("AnalysisResult")
    rows = analysis.get("Data") if isinstance(analysis, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceError("TLS 分析结果结构无法核验。")
    return rows


def _number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise SourceError("TLS 计数或时间字段不是非负整数。")
    return int(value)


def _sql_string(value):
    return "'" + value.replace("'", "''") + "'"


LOGGER = "coalesce(regexp_extract(__content__, '([A-Za-z_$][A-Za-z0-9_.$]+)(?::[0-9]+)? - ', 1),'')"
EXCEPTION = "coalesce(regexp_extract(__content__, '([A-Za-z_$][A-Za-z0-9_.$]*(?:Exception|Error))', 1),'')"
# Remove dynamic IDs while preserving the message, dependency and error name.
MESSAGE = (
    "substr(regexp_replace(regexp_replace(regexp_replace(regexp_replace("
    "coalesce(regexp_extract(__content__, ' - ([^\\r\\n]*)', 1),"
    "regexp_extract(__content__, '([^\\r\\n]*)', 1)),"
    "'([A-Za-z_$][A-Za-z0-9_.$]+)@[0-9a-fA-F]+', '$1@<object>'),"
    "'[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}', '<id>'),"
    "'[0-9]+', '<n>'), '[[:space:]]+', ' '), 1, 240)"
)


def _group_query(service, limit):
    return (f"* | select {LOGGER} as logger, {EXCEPTION} as exception, {MESSAGE} as template, "
            "count(*) as n, min(__content__) as example, "
            "min(__time__) as first_time, max(__time__) as last_time "
            f"where topic = {_sql_string(service)} "
            f"group by logger, exception, template order by n desc, logger, exception, template limit {limit}")


def _group_rows(client, topic_id, start, end, service, limit):
    for attempt in range(3):
        try:
            return _rows(client, topic_id, start, end, _group_query(service, limit))
        except SourceError as error:
            if not error.retryable or attempt == 2:
                raise
            time.sleep((2, 5)[attempt])


def _redact(value):
    text = str(value)
    text = re.sub(r"(?i)((?:authorization|password|passwd|secret|access[_-]?key|api[_-]?key|token)"
                  r"\s*[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s\"',;&]+", r"\1[REDACTED]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", text)
    text = re.sub(r"\b\d{15,19}[Xx]?\b", "[LONG_ID]", text)
    return text[:8000]


def collect_report(start, end, services, *, client=None, group_limit=GROUP_LIMIT, progress=None):
    """Return exact per-service counts and bounded, explicitly covered SQL groups.

    ``services`` is the verified platform inventory list. TLS's record field
    ``topic`` is compared exactly with each inventory service name; the TLS
    resource itself is named ``error``. No inferred aliases or prefix matches.
    """
    start, end = _seconds(start), _seconds(end)
    if start >= end or end - start > 86400 or not 1 <= group_limit <= MAX_GROUP_LIMIT:
        raise ValueError("Invalid TLS report window or group limit")
    names = [item.get("name") if isinstance(item, dict) else item for item in services]
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise SourceError("平台服务清单缺失或无效。")
    if len(names) != len(set(names)):
        raise SourceError("平台服务名重复，无法唯一绑定日志归属。")
    client = client or TlsClient()
    project, topic, index = resolve_topic(client)
    topic_id = topic["TopicId"]
    totals, previous = {}, None
    for _page in range(200):
        after = "" if previous is None else f"where topic > {_sql_string(previous[-1])} "
        query = (f"* | select topic, count(*) as n {after}group by topic order by topic "
                 f"limit {COUNT_PAGE_SIZE}")
        rows = _rows(client, topic_id, start, end, query)
        # A recursively split count page can contain duplicate service names.
        # Pagination over independently split windows is not stable; fail closed
        # if a page reaches its bound, instead of claiming missing rows are zero.
        if len(rows) > COUNT_PAGE_SIZE:
            raise SourceError("TLS 服务统计需缩小时间范围后重新分页。")
        for row in rows:
            service = row.get("topic")
            if not isinstance(service, str) or service in totals:
                raise SourceError("TLS 服务统计分页重复或归属字段缺失。")
            totals[service] = _number(row.get("n"))
        current = tuple(row.get("topic") for row in rows)
        if previous == current and rows:
            raise SourceError("TLS 服务统计分页未推进。")
        previous = current
        if len(rows) < COUNT_PAGE_SIZE:
            break
    else:
        raise SourceError("TLS 服务统计超过分页保护上限。")
    selected = {name: totals[name] for name in sorted(names) if totals.get(name, 0)}
    groups, limitations, failed_services, service_coverage = [], [], [], []
    for position, (service, expected) in enumerate(selected.items(), 1):
        began = time.monotonic()
        try:
            rows = _group_rows(client, topic_id, start, end, service, group_limit)
        except SourceError:
            failed_services.append(service)
            service_coverage.append({"service": service, "total_errors": expected,
                                     "grouped_errors": 0, "complete": False})
            if progress:
                progress({"phase": "tls_grouping", "completed": position, "total": len(selected),
                          "service": service, "elapsed_ms": int((time.monotonic() - began) * 1000),
                          "success": False})
            continue
        merged = {}
        for row in rows:
            if any(not isinstance(row.get(key), str) for key in ("logger", "exception", "template", "example")):
                raise SourceError("TLS 日志分组字段缺失。")
            key = (service, row["logger"], row["exception"], row["template"])
            if key not in merged:
                digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]
                merged[key] = {"id": digest, "service": service, "count": 0,
                               "logger": _redact(row["logger"]), "exception": _redact(row["exception"]),
                               "summary": _redact(row["template"]), "examples": [], "samples": [],
                               "first_time": _number(row.get("first_time")),
                               "last_time": _number(row.get("last_time"))}
            group = merged[key]
            group["count"] += _number(row.get("n"))
            group["first_time"] = min(group["first_time"], _number(row.get("first_time")))
            group["last_time"] = max(group["last_time"], _number(row.get("last_time")))
            sample = extract_sample(row["example"], service, source="tls_log")
            if sample and sample not in group["samples"] and len(group["samples"]) < 2:
                group["samples"].append(sample)
            example = _redact(row["example"])
            if example not in group["examples"] and len(group["examples"]) < 2:
                group["examples"].append(example)
        included = sum(group["count"] for group in merged.values())
        if included != expected:
            limitations.append(f"{service}：全窗计数 {expected}，分组覆盖 {included}；"
                               "分组上限或查询期间补入日志造成差额，未宣称完整归因。")
        service_coverage.append({"service": service, "total_errors": expected,
                                 "grouped_errors": included, "complete": included == expected})
        groups.extend(merged.values())
        if progress:
            progress({"phase": "tls_grouping", "completed": position, "total": len(selected),
                      "service": service, "elapsed_ms": int((time.monotonic() - began) * 1000),
                      "success": True, "groups": len(merged), "grouped_errors": included})
    if failed_services:
        limitations.append("分组查询未完成：" + "、".join(failed_services))
    total = sum(selected.values())
    grouped = sum(group["count"] for group in groups)
    return {
        "groups": sorted(groups, key=lambda group: (-group["count"], group["service"], group["id"])),
        "coverage": {
            "region": REGION, "project": PROJECT_NAME, "project_id": project["ProjectId"],
            "queried_topics": [{"name": TOPIC_NAME, "id": topic_id}],
            "start": start, "end": end, "total_errors": total, "grouped_errors": grouped,
            "ungrouped_errors": max(0, total - grouped), "total_services": len(names),
            "matched_services": len(selected), "service_counts": selected,
            "services_without_errors": len(names) - len(selected),
            "complete": all(item["complete"] for item in service_coverage),
            "limitations": limitations, "service_coverage": service_coverage,
            "sample_policy": "每个调用点/异常/归一化消息组最多2条代表样例；组内数量为SQL全窗计数，样例不代表所有根因。",
            "sample_index_max_text_length": index.get("MaxTextLen"),
        },
    }
