import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from scripts import run_tls_error_report as runner
from scripts import tls_report_notify as notifications


@pytest.fixture(autouse=True)
def no_live_locator_or_notification_calls(monkeypatch):
    # These seams must stay stubbed even in tests that exercise main() failures.
    monkeypatch.setattr(runner, "enrich_finding_locators", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner, "notify_report", lambda *args, **kwargs: {"status": "sent"})


def group(service="account-service", count=7, summary="Read timed out", identifier="raw-one"):
    return {"id": identifier, "service": service, "count": count,
            "logger": "AccountClient", "exception": "SocketTimeoutException",
            "summary": summary, "examples": [f"SocketTimeoutException: {summary}"]}


def collected(groups=None, *, total=None, complete=True):
    groups = [group()] if groups is None else groups
    counts = {}
    for row in groups:
        counts[row["service"]] = counts.get(row["service"], 0) + row["count"]
    grouped = sum(counts.values())
    total = grouped if total is None else total
    return {"groups": groups, "coverage": {
        "start": datetime(2026, 10, 20, 8, tzinfo=UTC).timestamp() * 1000,
        "end": datetime(2026, 10, 21, 8, tzinfo=UTC).timestamp() * 1000,
        "total_errors": total, "grouped_errors": grouped, "ungrouped_errors": total - grouped,
        "matched_services": len(counts), "service_counts": counts, "complete": complete,
        "queried_topics": [{"id": "topic-test"}], "sample_index_max_text_length": 2048,
    }}


def result_for(payload):
    row = payload["groups"][0]
    return {"summary": "日志出现读取超时；具体下游故障待核实。",
            "findings": [{
                "title": "下游读取超时", "priority": "P2",
                "group_ids": [item["id"] for item in payload["groups"]],
                "direct_cause": "调用在读取响应时超过客户端超时阈值。",
                "root_cause": "缺少对端同时间窗口指标，尚未证实底层原因。",
                "cause_status": "unverified", "actions": ["对齐 trace 与对端请求时延后定位慢请求。"],
                "evidence": [{"source_id": f"log:{row['id']}", "quote": row["summary"]}],
            }], "model": "gpt-6-sol", "reasoning_effort": "xhigh"}


@pytest.mark.parametrize("now,day,expected", [
    ("2026-10-21T15:59:59+08:00", None, "2026-10-20T16:00:00+08:00"),
    ("2026-10-21T16:00:00+08:00", None, "2026-10-21T16:00:00+08:00"),
    ("2026-10-21T23:59:59+08:00", None, "2026-10-21T16:00:00+08:00"),
    ("2026-10-22T00:01:00+08:00", None, "2026-10-21T16:00:00+08:00"),
    ("2026-10-21T08:00:00+00:00", None, "2026-10-21T16:00:00+08:00"),
    ("2026-10-23T08:00:00+00:00", "2026-10-20", "2026-10-20T16:00:00+08:00"),
])
def test_report_window_is_completed_shanghai_1600_not_rolling_now(now, day, expected):
    start, end = runner.report_window(day, now=datetime.fromisoformat(now))
    assert end.isoformat() == expected
    assert end - start == timedelta(hours=24)


def test_report_window_rejects_future_or_timezone_naive_clock():
    with pytest.raises(ValueError, match="has not completed"):
        runner.report_window("2026-10-21", now=datetime(2026, 10, 21, 7, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone"):
        runner.report_window(now=datetime(2026, 10, 21, 16))


def test_merging_counts_same_signature_across_services_preserves_distinct_mechanisms():
    rows = [group("alpha", 4), group("beta", 7), group("alpha", 2),
            group("beta", 3, "Unknown column missing_key")]
    original = deepcopy(rows)
    merged = runner.model_groups(rows)
    assert [item["count"] for item in merged] == [13, 3]
    assert merged[0]["service_counts"] == {"alpha": 6, "beta": 7}
    assert merged[0]["services"] == ["alpha", "beta"]
    assert len(merged[0]["examples"]) == 1
    assert sum(item["count"] for item in merged) == sum(item["count"] for item in rows)
    assert runner.model_groups(list(reversed(rows)))[0]["id"] == merged[0]["id"]
    assert rows == original


def test_java_object_identity_variations_merge_without_losing_original_evidence():
    first = group("alpha", 4, "Failed processing com.example.Payment@abcdef")
    second = group("beta", 6, "Failed processing com.example.Payment@<n>caf<n>e")
    third = group("alpha", 2, "Failed processing com.example.Refund@abcdef")
    merged = runner.model_groups([first, second, third])
    assert len(merged) == 2
    assert merged[0]["summary"] == "Failed processing com.example.Payment@<object>"
    assert merged[0]["count"] == 10
    assert merged[0]["service_counts"] == {"alpha": 4, "beta": 6}
    assert merged[0]["examples"] == [first["examples"][0], second["examples"][0]]
    assert merged[1]["summary"] == "Failed processing com.example.Refund@<object>"


def test_stack_continuations_group_as_log_lines_without_absorbing_exception_headers():
    rows = [group("alpha", 4, "\tat com.example.Client.send(Client.java:<n>)"),
            group("beta", 3, "    at com.example.Other.read(Other.java:<n>)"),
            group("beta", 2, "... <n> common frames omitted"),
            group("beta", 1, "... <n> frames omitted"),
            group("alpha", 5, "Caused by: java.net.SocketTimeoutException: Read timed out")]
    merged = runner.model_groups(rows)
    stack = next(row for row in merged if row["summary"].startswith("堆栈续行"))
    assert len(merged) == 2
    assert stack["count"] == 10
    assert stack["service_counts"] == {"alpha": 4, "beta": 6}
    assert "不单独代表一个异常事件" in stack["summary"]
    assert any(row["summary"].startswith("Caused by:") for row in merged)
    assert sum(row["count"] for row in merged) == 15


@pytest.mark.parametrize("message", [
    "Unknown column 'billing_mode' in field list",
    "Unknown column 'payment_mode' in field list",
    "Connection timed out to mysql.internal:3306",
    "Connection timed out to redis.internal:6379",
    "java.lang.IllegalArgumentException: invalid state",
])
def test_summary_normalization_preserves_meaningful_sql_dependency_and_exception_details(message):
    assert runner.normalize_summary(message) == message


def test_compact_payload_keeps_canonical_redacted_evidence_and_exact_group_identity():
    raw = group(summary="Read timed out")
    raw["examples"] = ["Read timed out; password=FAKE_PASSWORD", "Read timed out; phone=13812345678"]
    groups = runner.model_groups([raw])
    original = deepcopy(groups)
    payload = runner.compact_payload(groups, {"status": "partial"}, {"total_errors": 7})
    identifier = groups[0]["id"]
    assert payload["groups"] == [{"id": identifier, "count": 7, "summary": "Read timed out", "services": ["account-service"]}]
    source = payload["sources"][f"log:{identifier}"]
    assert source["logger"] == groups[0]["logger"]
    assert source["service_counts"] == {"account-service": 7}
    assert len(source["examples"]) == 2
    assert "FAKE_PASSWORD" not in json.dumps(payload)
    assert "13812345678" not in json.dumps(payload)
    assert json.dumps(payload).count("Read timed out; password=") == 1
    result = result_for(payload)
    result["findings"][0]["evidence"][0]["quote"] = source["examples"][0]
    assert runner.validate_analysis(result, payload)["findings"][0]["group_ids"] == [identifier]
    assert groups == original


def test_compact_payload_increases_sample_coverage_within_same_byte_budget():
    rows = []
    for index in range(20):
        row = group("service", 100 - index, f"Failure mechanism {index}")
        row["examples"] = ["Representative A " + "x " * 790, "Representative B " + "y " * 790]
        rows.append(row)
    data = collected(rows)
    full = runner.source_payload(runner.model_groups(rows), {}, scope=data["coverage"])
    assert len(json.dumps(full, ensure_ascii=False).encode()) > 118000
    payload, coverage = runner.prepare_payload(data, {})
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 118000
    assert coverage["analyzed_groups"] == coverage["template_groups"] == 20
    assert coverage["analyzed_errors"] == sum(row["count"] for row in rows)
    assert all(len(payload["sources"][f"log:{row['id']}"]["examples"]) == 2 for row in payload["groups"])


def test_model_budget_caps_groups_and_reports_exact_analyzed_error_count():
    rows = [group("service", 1000 - index, f"Distinct failure number {index}") for index in range(85)]
    data = collected(rows)
    payload, coverage = runner.prepare_payload(data, {})
    assert coverage["template_groups"] == 85
    assert coverage["analyzed_groups"] == 80
    assert coverage["analyzed_errors"] == sum(1000 - index for index in range(80))
    assert len(payload["groups"]) == 80
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 118000
    rendered = runner.render_report("2026-10-21", data, {}, result_for(payload), coverage)
    assert "其余日志仅统计，未逐项归因" in rendered
    assert f"覆盖 {coverage['analyzed_errors']:,} 条" in rendered


def test_model_budget_uses_utf8_bytes_and_cannot_silently_analyze_nothing():
    data = collected([group(summary="中文异常 " * 40000)])
    with pytest.raises(ValueError, match="exceeds the analysis input limit"):
        runner.prepare_payload(data, {})


def test_partial_tls_coverage_is_explicit_even_if_all_available_groups_analyzed():
    data = collected(total=20, complete=False)
    payload, coverage = runner.prepare_payload(data, {})
    text = runner.render_report("2026-10-21", data, {}, result_for(payload), coverage)
    assert "TLS 分组未完整覆盖：13 条尚未归并" in text
    assert "20 条错误" in text
    assert "2048 字节" in text


def test_render_recomputes_category_totals_and_escapes_untrusted_xml():
    data = collected([group('pay<&"service', 4), group("beta", 8)])
    evidence = {"status": "available", "services": {"beta": {}},
                "grafana_dashboard": 'https://grafana.example.test/d/one?from=1&to=2" onmouseover="bad'}
    payload, coverage = runner.prepare_payload(data, evidence)
    result = result_for(payload)
    result["summary"] = '<script>alert("bad")</script> & text'
    for field in ("title", "direct_cause", "root_cause"):
        result["findings"][0][field] = f'<b data-x="1">{field}</b>&'
    result["findings"][0]["actions"] = ["<a>动作&建议</a>"]
    result["findings"][0]["evidence"][0]["quote"] = "<tag>原始日志&样本</tag>"
    body = runner.render_report("2026-10-21", data, evidence, result, coverage)
    xml = ET.fromstring("<root>" + body + "</root>")
    assert "｜12 条" in body
    assert xml.findall(".//script") == []
    assert xml.findall(".//tag") == []
    assert xml.find(".//a").attrib == {"href": evidence["grafana_dashboard"]}
    assert '&lt;script&gt;' in body and 'pay&lt;&amp;&quot;service' in body


def test_render_removes_xml_forbidden_chars_and_does_not_claim_missing_metrics_prove_cause():
    data = collected()
    evidence = {"status": "partial", "services": {}}
    payload, coverage = runner.prepare_payload(data, evidence)
    result = result_for(payload)
    result["summary"] = "日志\x00结论\x01；保留换行\n和中文\ud800"
    result["findings"][0]["cause_status"] = "confirmed"
    body = runner.render_report("2026-10-21", data, evidence, result, coverage)
    ET.fromstring("<root>" + body + "</root>")
    assert all(char not in body for char in ("\x00", "\x01", "\ud800"))
    assert "保留换行\n和中文" in body
    assert "未据缺失指标推断根因" in body
    assert "根因（已确认）" in body


@pytest.fixture
def job(tmp_path, monkeypatch):
    start = datetime(2026, 10, 20, 8, tzinfo=UTC).astimezone(runner.ZONE)
    end = start + timedelta(days=1)
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner, "report_window", lambda day: (start, end))
    calls = {name: 0 for name in ("inventory", "collect", "evidence", "analyze", "publish")}
    state = {"data": collected(), "analysis_failure": False, "publish_failure": False,
             "notification_prepare_failure": False, "notification_send_failure": False}
    location_calls, notification_calls, sent_messages = [], [], []

    def inventory(cockpit):
        calls["inventory"] += 1
        return {"updated_at": "2026-10-21T15:00:00+08:00", "services": [{"name": "account-service"}]}

    def collect(begin, finish, services):
        calls["collect"] += 1
        assert begin == start and finish == end
        return deepcopy(state["data"])

    def evidence(names, begin, finish):
        calls["evidence"] += 1
        assert begin == start and finish == end
        return {"status": "available", "services": {name: {} for name in names}}

    def analyze(payload):
        calls["analyze"] += 1
        if state["analysis_failure"]:
            raise ValueError("model unavailable")
        return result_for(payload)

    def publish(day, body):
        calls["publish"] += 1
        assert day == "2026-10-21"
        ET.fromstring("<root>" + body + "</root>")
        if state["publish_failure"]:
            raise ValueError("publisher unavailable")
        return {"url": "https://atrenew.feishu.cn/wiki/testreport", "document_id": "testreport"}

    def locators(findings, groups, coverage):
        location_calls.append(deepcopy(findings))
        return {runner.finding_key(finding): {
            "status": "verified", "service": groups[0]["services"][0],
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "query": 'topic:"account-service" AND "SocketTimeoutException"',
        } for finding in findings}

    class Transport:
        def prepare(self):
            if state["notification_prepare_failure"]:
                raise notifications.NotificationError("notification_transport_unavailable")
            return {"owner": "ou_testowner", "identity": "a" * 64}

        def send(self, text, identifier, identity):
            sent_messages.append({"text": text, "uuid": identifier, "identity": identity})
            if state["notification_send_failure"]:
                raise notifications.NotificationError("notification_transport_unavailable")
            return {"message_id": "om_test" + str(len(sent_messages))}

    def notify(day, result):
        notification_calls.append(deepcopy(result))
        if result["status"] in {"published", "already_published"}:
            saved = json.loads((tmp_path / "days" / day / "published.json").read_text())
            assert saved["content_hash"] == result["content_hash"]
        return notifications.notify_report(day, result, state_dir=tmp_path / "notifications", transport=Transport())

    monkeypatch.setattr(runner, "inventory", inventory)
    monkeypatch.setattr(runner, "collect_report", collect)
    monkeypatch.setattr(runner, "collect_evidence", evidence)
    monkeypatch.setattr(runner, "analyze", analyze)
    monkeypatch.setattr(runner, "publish_report", publish)
    monkeypatch.setattr(runner, "enrich_finding_locators", locators)
    monkeypatch.setattr(runner, "notify_report", notify)
    return {"calls": calls, "state": state, "folder": tmp_path / "days" / "2026-10-21", "runtime": tmp_path,
            "location_calls": location_calls, "notification_calls": notification_calls, "sent_messages": sent_messages}


def test_completed_day_is_idempotent_without_new_collection_model_or_publication(job):
    first = runner.run()
    second = runner.run()
    assert first["status"] == "published"
    assert second["status"] == "already_published"
    assert first["url"] == second["url"]
    assert job["calls"] == {"inventory": 1, "collect": 1, "evidence": 1, "analyze": 1, "publish": 1}


def test_collect_only_caches_evidence_but_does_not_run_model_or_publish(job):
    first = runner.run(collect_only=True)
    assert first["status"] == "collected"
    assert not (job["folder"] / "published.json").exists()
    assert job["calls"]["analyze"] == job["calls"]["publish"] == 0
    second = runner.run()
    assert second["status"] == "published"
    assert job["calls"] == {"inventory": 1, "collect": 1, "evidence": 1, "analyze": 1, "publish": 1}


def test_publication_failure_retries_saved_analysis_without_recollecting(job):
    job["state"]["publish_failure"] = True
    with pytest.raises(ValueError, match="publisher unavailable"):
        runner.run()
    assert (job["folder"] / "analysis.json").exists()
    assert not (job["folder"] / "published.json").exists()
    job["state"]["publish_failure"] = False
    assert runner.run()["status"] == "published"
    assert job["calls"] == {"inventory": 1, "collect": 1, "evidence": 1, "analyze": 1, "publish": 2}


def test_model_failure_never_publishes_partial_unvalidated_findings(job):
    job["state"]["analysis_failure"] = True
    with pytest.raises(ValueError, match="model unavailable"):
        runner.run()
    assert not (job["folder"] / "analysis.json").exists()
    assert not (job["folder"] / "published.json").exists()
    assert job["calls"]["publish"] == 0
    job["state"]["analysis_failure"] = False
    assert runner.run()["status"] == "published"
    assert job["calls"]["collect"] == 1
    assert job["calls"]["analyze"] == 2


def test_invalid_cached_analysis_is_rejected_before_publication(job):
    runner.run(collect_only=True)
    payload = json.loads((job["folder"] / "analysis-input.json").read_text())
    broken = result_for(payload)
    broken["findings"][0]["evidence"][0]["quote"] = "Invented root cause not present"
    (job["folder"] / "analysis.json").write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="Unverifiable evidence"):
        runner.run()
    assert job["calls"]["publish"] == job["calls"]["analyze"] == 0
    assert not (job["folder"] / "published.json").exists()


def test_source_failure_cannot_cache_empty_results_or_publish(job, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("TLS unavailable")

    monkeypatch.setattr(runner, "collect_report", fail)
    with pytest.raises(RuntimeError, match="TLS unavailable"):
        runner.run()
    assert not (job["folder"] / "collected.json").exists()
    assert not (job["folder"] / "published.json").exists()
    assert job["calls"]["analyze"] == job["calls"]["publish"] == 0


def test_no_errors_publishes_clear_zero_result_without_model(job):
    job["state"]["data"] = collected([])
    result = runner.run()
    assert result["errors"] == 0
    assert job["calls"]["analyze"] == 0
    assert "本窗口未检出平台研发服务错误日志" in (job["folder"] / "report.xml").read_text()


def test_cli_failure_status_does_not_expose_exception_details(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner.sys, "argv", ["run_tls_error_report.py"])

    def fail(*args, **kwargs):
        raise ValueError("private token and raw customer log")

    monkeypatch.setattr(runner, "run", fail)
    assert runner.main() == 1
    printed = capsys.readouterr().out
    stored = (tmp_path / "last-run.json").read_text()
    assert "private token" not in printed + stored
    assert json.loads(stored)["error_type"] == "ValueError"
    assert json.loads(stored)["status"] == "failed"


def test_execute_persists_publication_before_notifying_and_deduplicates_reruns(job):
    first = runner.execute()
    second = runner.execute()
    assert first["status"] == "published"
    assert second["status"] == "already_published"
    assert first["notification"]["status"] == second["notification"]["status"] == "sent"
    assert job["calls"]["publish"] == job["calls"]["analyze"] == 1
    assert len(job["location_calls"]) == 1
    assert len(job["sent_messages"]) == 1
    assert "日报已完成" in job["sent_messages"][0]["text"]


def test_notification_preflight_failure_retries_only_notification_not_the_report(job):
    job["state"]["notification_prepare_failure"] = True
    first = runner.execute()
    assert first["status"] == "published"
    assert first["notification"]["status"] == "failed"
    assert (job["folder"] / "published.json").exists()
    assert not job["sent_messages"]
    job["state"]["notification_prepare_failure"] = False
    second = runner.execute()
    assert second["status"] == "already_published"
    assert second["notification"]["status"] == "sent"
    assert job["calls"] == {"inventory": 1, "collect": 1, "evidence": 1, "analyze": 1, "publish": 1}
    assert len(job["location_calls"]) == len(job["sent_messages"]) == 1


def test_notification_unknown_result_is_not_resent_and_does_not_erase_publication(job):
    job["state"]["notification_send_failure"] = True
    first = runner.execute()
    job["state"]["notification_send_failure"] = False
    second = runner.execute()
    assert first["status"] == "published"
    assert second["status"] == "already_published"
    assert first["notification"]["status"] == second["notification"]["status"] == "unknown"
    assert len(job["sent_messages"]) == 1
    assert job["calls"]["publish"] == 1


def test_execute_notification_exception_is_not_recursively_notified(job, monkeypatch):
    calls = []

    def fail(day, result):
        calls.append(result["status"])
        raise RuntimeError("private transport details")

    monkeypatch.setattr(runner, "notify_report", fail)
    result = runner.execute()
    assert result["status"] == "published"
    assert result["notification"] == {"status": "failed", "error_type": "RuntimeError"}
    assert calls == ["published"]
    assert "private transport details" not in json.dumps(result)
    assert (job["folder"] / "published.json").exists()


@pytest.mark.parametrize("stage", ["collect", "analyze", "publish"])
def test_execute_failure_is_notified_once_then_success_sends_separate_notice(job, monkeypatch, stage):
    original = runner.collect_report
    if stage == "collect":
        def fail(*args, **kwargs):
            raise ValueError("private source details")
        monkeypatch.setattr(runner, "collect_report", fail)
    else:
        job["state"][f"{stage if stage != 'analyze' else 'analysis'}_failure"] = True
    first, duplicate = runner.execute(), runner.execute()
    assert first["status"] == duplicate["status"] == "failed"
    assert first["notification"]["status"] == "sent"
    assert len(job["sent_messages"]) == 1
    assert "日报未完成" in job["sent_messages"][0]["text"]
    assert "private" not in json.dumps(first) + job["sent_messages"][0]["text"]
    if stage == "collect":
        monkeypatch.setattr(runner, "collect_report", original)
    else:
        job["state"][f"{stage if stage != 'analyze' else 'analysis'}_failure"] = False
    success = runner.execute()
    assert success["status"] == "published"
    assert len(job["sent_messages"]) == 2
    assert "日报已完成" in job["sent_messages"][1]["text"]


def test_collect_only_never_notifies_before_or_after_publication_even_with_republish(job):
    first = runner.execute(collect_only=True)
    assert first["status"] == "collected"
    assert "notification" not in first
    assert not job["notification_calls"]
    runner.run()
    second = runner.execute(collect_only=True, republish=True)
    assert second["status"] == "already_published"
    assert "notification" not in second
    assert not job["notification_calls"]
    assert job["calls"]["publish"] == 1


def test_collect_only_failure_also_suppresses_notifications(job):
    job["state"]["data"] = {"coverage": {}}
    result = runner.execute(collect_only=True)
    assert result["status"] == "failed"
    assert "notification" not in result
    assert not job["notification_calls"]


def test_invalid_window_does_not_count_as_report_failure_or_send_message(job, monkeypatch):
    def unfinished(day):
        raise ValueError("Report window has not completed")

    monkeypatch.setattr(runner, "report_window", unfinished)
    with pytest.raises(ValueError, match="has not completed"):
        runner.execute("2099-01-01")
    assert not job["notification_calls"]
    assert job["calls"]["collect"] == 0


def test_republish_same_document_and_same_content_does_not_send_another_dm(job):
    first = runner.execute()
    repeated = runner.execute(republish=True)
    assert repeated["document_id"] == first["document_id"]
    assert repeated["content_hash"] == first["content_hash"]
    assert job["calls"]["publish"] == 2
    assert job["calls"]["analyze"] == job["calls"]["collect"] == 1
    assert len(job["location_calls"]) == len(job["sent_messages"]) == 1


def test_republish_changed_locator_updates_original_document_and_sends_new_revision_notice(job):
    first = runner.execute()
    path = job["folder"] / "locators.json"
    locators = json.loads(path.read_text())
    row = next(iter(locators.values()))
    row["swtid"] = "15092365925" + "a" * 21
    row["query"] += ' AND swtid:"' + row["swtid"] + '"'
    path.write_text(json.dumps(locators))
    changed = runner.execute(republish=True)
    assert changed["document_id"] == first["document_id"]
    assert changed["content_hash"] != first["content_hash"]
    assert len(job["sent_messages"]) == 2
    assert row["swtid"] in (job["folder"] / "report.xml").read_text()
    runner.execute(republish=True)
    assert len(job["sent_messages"]) == 2
    assert job["calls"]["analyze"] == job["calls"]["collect"] == 1


def test_failed_republish_preserves_previous_confirmed_document_record(job):
    first = runner.execute()
    path = job["folder"] / "published.json"
    saved = path.read_text()
    job["state"]["publish_failure"] = True
    result = runner.execute(republish=True)
    assert result["status"] == "failed"
    assert path.read_text() == saved
    assert json.loads(saved)["document_id"] == first["document_id"]
    assert len(job["sent_messages"]) == 2
    assert "已有日报保留" in job["sent_messages"][-1]["text"]


@pytest.mark.parametrize("notification_status,exit_code", [("failed", 1), ("unknown", 1), ("sent", 0)])
def test_main_retains_published_status_and_reports_notification_exit_code(job, monkeypatch, capsys, notification_status, exit_code):
    monkeypatch.setattr(runner.sys, "argv", ["run_tls_error_report.py", "--republish"])
    monkeypatch.setattr(runner, "notify_report", lambda *args: {"status": notification_status})
    assert runner.main() == exit_code
    state = json.loads((job["runtime"] / "last-run.json").read_text())
    assert state["status"] == "published"
    assert state["notification"]["status"] == notification_status
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "published"


def test_render_locator_uses_shanghai_time_and_escapes_query_without_changing_copyable_text():
    swtid = "15092365925" + "a" * 21
    query = 'topic:"pay<&service" AND swtid:"' + swtid + '" AND "Exception <bad> & failure"'
    locator = {"status": "verified", "service": "pay<&service", "swtid": swtid,
               "window_start": "2026-10-21T08:00:00+00:00", "window_end": "2026-10-21T08:10:00+00:00",
               "time_ms": int(datetime(2026, 10, 21, 8, 5, tzinfo=UTC).timestamp() * 1000),
               "query": query}
    body = runner.render_locator(locator)
    xml = ET.fromstring("<root>" + body + "</root>")
    assert xml.find("pre/code").text == query
    assert "2026-10-21 16:00:00—2026-10-21 16:10:00" in body
    assert "样例时间：10-21 16:05:00" in body
    assert f"样例 swtid：{swtid}" in body
    assert "&lt;bad&gt;" in body and xml.findall(".//bad") == []
    assert "[PHONE]" not in body


def test_verified_locator_without_swtid_only_shows_verified_fallback_query():
    locator = {"status": "verified", "service": "account-service", "query": 'topic:"account-service" AND "SocketTimeoutException"',
               "window_start": 1792569600000, "window_end": 1792570200000}
    body = runner.render_locator(locator)
    assert "样例 swtid" not in body
    assert "TLS 查询：account-service" in body
    assert "SocketTimeoutException" in body


@pytest.mark.parametrize("locator", [None, {}, {"status": "unavailable", "swtid": "fake", "query": "invented"}])
def test_unverified_locator_does_not_render_fabricated_swtid_or_query(locator):
    body = runner.render_locator(locator)
    assert "定位查询暂未核验" in body
    assert "fake" not in body and "invented" not in body and "<pre" not in body
