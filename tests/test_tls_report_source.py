from datetime import UTC, datetime

import pytest

from scripts.tls_report_source import SourceError, _rows, collect_report


def result(rows, status="complete"):
    return {"ResultStatus": status, "AnalysisResult": {"Data": rows}}


def group(count=4, **changes):
    return {"logger": "com.example.Service", "exception": "NullPointerException",
            "template": "cannot load user <n>", "n": str(count),
            "example": "NullPointerException token=abc123 phone=13812345678",
            "first_time": "100000", "last_time": "200000", **changes}


class FakeTls:
    def __init__(self, *, counts=None, rows=None, fail_groups=False):
        self.counts = counts if counts is not None else [
            {"topic": "platform-service", "n": "4"},
            {"topic": "platform-service-copy", "n": "99"},
            {"topic": "other", "n": "100"},
        ]
        self.rows = rows if rows is not None else [group()]
        self.calls = []
        self.fail_groups = fail_groups

    def call(self, action, inputs):
        self.calls.append((action, inputs))
        if action == "project.describe-projects":
            return {"Total": 1, "Projects": [{"ProjectId": "project-id",
                                               "ProjectName": "pro-jd-sh-service"}]}
        if action == "topic.describe-topics":
            return {"Total": 1, "Topics": [{"ProjectId": "project-id", "TopicId": "error-id",
                                             "TopicName": "error"}]}
        if action == "index.describe":
            return {"KeyValue": [{"Key": "topic", "Value": {"SqlFlag": True}}],
                    "UserInnerKeyValue": [{"Key": "__content__", "Value": {"SqlFlag": True}}],
                    "MaxTextLen": 2048}
        assert action == "log.search"
        assert inputs["MustComplete"] is True
        if "group by topic" in inputs["Query"]:
            return result(self.counts)
        if self.fail_groups:
            raise SourceError("unavailable")
        return result(self.rows)


def test_exact_ownership_counts_and_redacted_examples():
    client = FakeTls()
    report = collect_report(1, 1000, [{"name": "platform-service"}, {"name": "quiet"}],
                            client=client)
    coverage = report["coverage"]
    assert coverage["complete"] is True
    assert coverage["total_errors"] == coverage["grouped_errors"] == 4
    assert coverage["matched_services"] == coverage["services_without_errors"] == 1
    assert coverage["sample_index_max_text_length"] == 2048
    entry = report["groups"][0]
    assert entry["service"] == "platform-service"
    assert "abc123" not in entry["examples"][0]
    assert "13812345678" not in entry["examples"][0]
    queries = [data["Query"] for action, data in client.calls if action == "log.search"]
    assert "where topic = 'platform-service'" in queries[-1]
    assert "platform-service-copy" not in queries[-1]


def test_group_limit_exposes_exact_unclassified_remainder():
    client = FakeTls(rows=[group(2)])
    report = collect_report(1, 1000, ["platform-service"], client=client, group_limit=1)
    assert report["coverage"]["total_errors"] == 4
    assert report["coverage"]["ungrouped_errors"] == 2
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["limitations"]


def test_failed_groups_remain_visible_not_no_error():
    report = collect_report(1, 1000, ["platform-service"], client=FakeTls(fail_groups=True))
    assert report["groups"] == []
    assert report["coverage"]["total_errors"] == 4
    assert report["coverage"]["ungrouped_errors"] == 4
    assert report["coverage"]["complete"] is False


def test_ingestion_drift_is_incomplete_even_if_all_group_rows_return():
    report = collect_report(1, 1000, ["platform-service"], client=FakeTls(rows=[group(5)]))
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["total_errors"] == 4
    assert report["coverage"]["grouped_errors"] == 5


def test_no_matching_services_is_complete_zero():
    client = FakeTls()
    report = collect_report(1, 1000, ["another-platform-service"], client=client)
    assert report["coverage"]["complete"] is True
    assert report["coverage"]["total_errors"] == 0
    assert report["groups"] == []
    assert sum(action == "log.search" for action, _ in client.calls) == 1


def test_stable_group_id_across_count_and_time_changes():
    first = collect_report(1, 1000, ["platform-service"], client=FakeTls())
    second = collect_report(1, 2000, ["platform-service"], client=FakeTls(rows=[group(3)]))
    assert first["groups"][0]["id"] == second["groups"][0]["id"]


def test_incomplete_scan_discards_partial_counts_and_narrows():
    class Partial:
        def __init__(self):
            self.calls = []

        def call(self, action, inputs):
            self.calls.append((inputs["StartTime"], inputs["EndTime"]))
            if inputs["EndTime"] - inputs["StartTime"] > 60:
                return result([group(999)], "incomplete")
            return result([group(1)])

    client = Partial()
    rows = _rows(client, "error-id", 0, 120, "query")
    assert [row["n"] for row in rows] == ["1", "1"]
    assert client.calls == [(0, 120), (0, 60), (60, 120)]


def test_incomplete_small_scan_fails_closed():
    class Partial:
        def call(self, action, inputs):
            return result([], "incomplete")

    with pytest.raises(SourceError, match="扫描未完成"):
        _rows(Partial(), "error-id", 0, 60, "query")


def test_duplicate_names_and_invalid_windows_rejected_before_query():
    with pytest.raises(SourceError, match="重复"):
        collect_report(1, 1000, ["a", "a"], client=FakeTls())
    with pytest.raises(ValueError):
        collect_report(datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC), ["a"])
    with pytest.raises(ValueError):
        collect_report(1, 100000, ["a"])


def test_invalid_numeric_count_does_not_become_zero():
    with pytest.raises(SourceError, match="非负整数"):
        collect_report(1, 1000, ["a"], client=FakeTls(counts=[{"topic": "a", "n": "unknown"}]))


def test_duplicate_count_rows_rejected():
    with pytest.raises(SourceError, match="分页重复"):
        collect_report(1, 1000, ["a"], client=FakeTls(counts=[
            {"topic": "a", "n": "1"}, {"topic": "a", "n": "1"},
        ]))


def test_service_count_keyset_pagination_reaches_later_platform_service(monkeypatch):
    monkeypatch.setattr("scripts.tls_report_source.COUNT_PAGE_SIZE", 2)

    class Paged(FakeTls):
        def call(self, action, inputs):
            if action == "log.search" and "group by topic" in inputs["Query"]:
                self.calls.append((action, inputs))
                if "where topic > 'b'" in inputs["Query"]:
                    return result([{"topic": "platform-service", "n": "4"}])
                return result([{"topic": "a", "n": "1"}, {"topic": "b", "n": "2"}])
            return super().call(action, inputs)

    client = Paged()
    report = collect_report(1, 1000, ["platform-service"], client=client)
    assert report["coverage"]["total_errors"] == 4
    assert report["coverage"]["complete"] is True
    assert any("where topic > 'b'" in inputs.get("Query", "") for _, inputs in client.calls)


def test_catalog_project_must_match_exactly():
    class WrongProject(FakeTls):
        def call(self, action, inputs):
            if action == "project.describe-projects":
                return {"Total": 1, "Projects": [{"ProjectId": "project-id",
                                                   "ProjectName": "pro-jd-sh-service-copy"}]}
            return super().call(action, inputs)

    with pytest.raises(SourceError, match="唯一确认"):
        collect_report(1, 1000, ["platform-service"], client=WrongProject())


def test_missing_sql_index_does_not_report_zero_errors():
    class BadIndex(FakeTls):
        def call(self, action, inputs):
            if action == "index.describe":
                return {"KeyValue": [], "UserInnerKeyValue": []}
            return super().call(action, inputs)

    with pytest.raises(SourceError, match="索引"):
        collect_report(1, 1000, ["platform-service"], client=BadIndex())


def test_service_query_escapes_sql_quote():
    service = "owned'service"
    client = FakeTls(counts=[{"topic": service, "n": "4"}])
    report = collect_report(1, 1000, [service], client=client)
    assert report["coverage"]["complete"] is True
    assert "where topic = 'owned''service'" in client.calls[-1][1]["Query"]


def test_transient_group_failure_retries_then_preserves_exact_count(monkeypatch):
    waits = []
    monkeypatch.setattr("scripts.tls_report_source.time.sleep", waits.append)

    class Transient(FakeTls):
        attempts = 0

        def call(self, action, inputs):
            if action == "log.search" and "group by logger" in inputs["Query"]:
                self.attempts += 1
                if self.attempts < 3:
                    raise SourceError("temporary", retryable=True)
            return super().call(action, inputs)

    client = Transient()
    report = collect_report(1, 1000, ["platform-service"], client=client)
    assert report["coverage"]["complete"] is True
    assert client.attempts == 3
    assert waits == [2, 5]


def test_transient_retries_are_bounded_and_never_claim_zero(monkeypatch):
    waits = []
    monkeypatch.setattr("scripts.tls_report_source.time.sleep", waits.append)

    class Unavailable(FakeTls):
        attempts = 0

        def call(self, action, inputs):
            if action == "log.search" and "group by logger" in inputs["Query"]:
                self.attempts += 1
                raise SourceError("temporary", retryable=True)
            return super().call(action, inputs)

    client = Unavailable()
    report = collect_report(1, 1000, ["platform-service"], client=client)
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["total_errors"] == 4
    assert client.attempts == 3
    assert waits == [2, 5]


def test_nontransient_group_failure_does_not_retry(monkeypatch):
    monkeypatch.setattr("scripts.tls_report_source.time.sleep", lambda _: pytest.fail("must not retry"))
    report = collect_report(1, 1000, ["platform-service"], client=FakeTls(fail_groups=True))
    assert report["coverage"]["complete"] is False


def test_progress_contains_counts_but_never_log_content():
    seen = []
    collect_report(1, 1000, ["platform-service"], client=FakeTls(), progress=seen.append)
    assert len(seen) == 1
    assert seen[0]["completed"] == seen[0]["total"] == 1
    assert seen[0]["grouped_errors"] == 4
    assert "abc123" not in str(seen)
    assert "examples" not in seen[0]


def test_larger_supported_sql_group_limit():
    client = FakeTls()
    collect_report(1, 1000, ["platform-service"], client=client, group_limit=10000)
    assert client.calls[-1][1]["Query"].endswith("limit 10000")
    with pytest.raises(ValueError):
        collect_report(1, 1000, ["platform-service"], client=client, group_limit=100001)


def test_collector_keeps_trace_metadata_before_body_redaction():
    trace = "a" * 10 + "13812345678" + "b" * 11
    text = f"2026-10-21 10:20:30.123 [worker] ERROR[{trace},0123456789abcdef] Service - failed"
    report = collect_report(1, 1000, ["platform-service"], client=FakeTls(rows=[group(example=text)]))
    row = report["groups"][0]
    assert trace not in row["examples"][0]
    assert row["samples"][0]["swtid"] == trace
    assert row["samples"][0]["source"] == "tls_log"
    assert row["samples"][0]["time_ms"] != row["first_time"]
