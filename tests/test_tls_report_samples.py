from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.tls_report_samples import (
    build_locator,
    enrich_finding_locators,
    extract_sample,
    extract_swtid,
    finding_key,
    samples_for_group,
)
from scripts.tls_report_source import _redact

TRACE = "4b960e9476637a883e7c924ba9b212a9"
CONTENT = f"2026-10-21 10:20:30.123 [worker-1] ERROR[{TRACE},d2723ba012345678],[,],normal Service - failed\njava.lang.IllegalStateException: example"
TIME = int(datetime(2026, 10, 21, 10, 20, 30, 123000, ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
COVERAGE = {"start": TIME // 1000 - 3600, "end": TIME // 1000 + 3600,
            "project": "pro-jd-sh-service", "region": "cn-shanghai",
            "queried_topics": [{"name": "error", "id": "error-id"}]}


def test_extracts_first_error_header_id_not_span_or_arbitrary_hash():
    assert extract_swtid(CONTENT) == TRACE
    assert extract_swtid("response hash=" + TRACE) is None
    assert extract_swtid("message contains ERROR[" + TRACE + ",span]") is None
    assert extract_swtid("ERROR[,0123456789abcdef],[,],normal") is None
    assert extract_swtid("ERROR[0123456789abcdef,0123456789abcdef],[,],normal") is None


def test_explicit_swtid_field_supported():
    assert extract_swtid({"swtid": TRACE, "__content__": "anything"}) == TRACE
    assert extract_swtid('{"swtid":"' + TRACE + '"}') == TRACE
    assert extract_swtid("swtid=" + TRACE) == TRACE
    assert extract_swtid({"trace_id": TRACE}) is None
    assert extract_swtid("swtid=" + TRACE + "-corrupt") is None


def test_time_and_trace_are_taken_from_same_raw_log():
    sample = extract_sample(CONTENT, "service-a", source="tls_log")
    assert sample["swtid"] == TRACE
    assert sample["time_ms"] == TIME
    assert sample["service"] == "service-a"
    assert sample["verified"] is True
    assert extract_sample({"topic": "other", "__content__": CONTENT}, "service-a") is None


def test_independent_metadata_survives_phone_shaped_trace_segment():
    trace = "a" * 10 + "13812345678" + "b" * 11
    raw = CONTENT.replace(TRACE, trace)
    sample = extract_sample(raw, "service-a", source="tls_log")
    assert trace not in _redact(raw)
    assert sample["swtid"] == trace
    assert extract_swtid(_redact(raw)) is None


def test_no_trace_uses_real_exception_and_does_not_invent_id():
    raw = CONTENT.replace(TRACE, "")
    sample = extract_sample(raw, "service-a")
    assert "swtid" not in sample
    assert sample["phrase"] == "java.lang.IllegalStateException"
    assert sample["time_ms"] == TIME


def test_historical_cache_recovers_sample_metadata():
    samples = samples_for_group({"service": "service-a", "examples": [CONTENT]})
    assert samples[0]["swtid"] == TRACE
    assert samples[0]["source"] == "cached_log"
    assert samples[0]["verified"] is False


def test_merged_group_does_not_guess_which_service_wrote_example():
    assert samples_for_group({"services": ["a", "b"], "examples": [CONTENT]}) == []


def test_query_uses_exact_service_trace_and_two_minute_window():
    locator = build_locator(extract_sample(CONTENT, "service-a"), COVERAGE)
    assert "topic = 'service-a'" in locator["query"]
    assert "like '%" + TRACE + "%'" in locator["query"]
    assert locator["window_start"] == TIME // 1000 - 60
    assert locator["window_end"] == TIME // 1000 + 61
    assert locator["time_ms"] == TIME


def test_query_with_no_trace_preserves_real_exception_phrase():
    locator = build_locator(extract_sample(CONTENT.replace(TRACE, ""), "a'b"), COVERAGE)
    assert "topic = 'a''b'" in locator["query"]
    assert "like '%java.lang.IllegalStateException%'" in locator["query"]
    assert locator["swtid"] is None


def test_corrupt_cached_swtid_is_never_returned_as_real_id():
    sample = extract_sample(CONTENT, "service-a")
    sample["swtid"] = "[REDACTED]"
    locator = build_locator(sample, COVERAGE)
    assert locator["swtid"] is None
    assert "REDACTED" not in locator["query"]


def test_query_window_clamped_to_report_window():
    sample = extract_sample(CONTENT, "service-a")
    coverage = {**COVERAGE, "start": TIME // 1000, "end": TIME // 1000 + 10}
    locator = build_locator(sample, coverage)
    assert (locator["window_start"], locator["window_end"]) == (coverage["start"], coverage["end"])


class FakeTls:
    def __init__(self, rows=None, status="complete"):
        self.calls = []
        self.rows = rows if rows is not None else [{"time": str(TIME), "service": "service-a", "content": CONTENT}]
        self.status = status

    def call(self, action, inputs):
        self.calls.append((action, inputs))
        return {"ResultStatus": self.status, "AnalysisResult": {"Data": self.rows}}


def finding_and_groups():
    finding = {"group_ids": ["group-a"]}
    group = {"id": "group-a", "services": ["service-a"], "count": 10,
             "samples": [extract_sample(CONTENT, "service-a")]}
    return finding, [group]


def test_locator_is_verified_only_after_real_query_matches():
    finding, groups = finding_and_groups()
    client = FakeTls()
    result = enrich_finding_locators([finding], groups, COVERAGE, client=client)[finding_key(finding)]
    assert result["status"] == "verified"
    assert result["validated_match_count"] == 1
    assert client.calls[0][1]["Query"] == result["query"]
    assert client.calls[0][1]["StartTime"] == result["window_start"]


@pytest.mark.parametrize("rows,status", [([], "complete"),
    ([{"service": "other", "content": CONTENT}], "complete"),
    ([{"service": "service-a", "content": "unrelated"}], "complete"),
    ([{"service": "service-a", "content": CONTENT}], "incomplete")])
def test_unmatched_or_incomplete_query_is_never_verified(rows, status):
    finding, groups = finding_and_groups()
    result = enrich_finding_locators([finding], groups, COVERAGE,
                                     client=FakeTls(rows, status))[finding_key(finding)]
    assert result["status"] == "unavailable"


def test_finding_id_is_not_used_as_trace():
    finding = {"group_ids": [TRACE]}
    group = {"id": TRACE, "service": "service-a", "count": 1, "examples": ["no trace or exception"]}
    client = FakeTls()
    result = enrich_finding_locators([finding], [group], COVERAGE, client=client)[finding_key(finding)]
    assert result["status"] == "unavailable"
    assert not client.calls


def test_wrapper_exception_prefers_specific_observed_diagnostic():
    text = CONTENT.replace(TRACE, "").replace("java.lang.IllegalStateException: example",
        "com.example.LeoException: invalid entry size (expected 12345 but got 100 bytes)")
    sample = extract_sample(text, "service-a")
    assert sample["phrase"] == "invalid entry size"
    assert "LeoException" not in build_locator(sample, COVERAGE)["query"]


def test_without_trace_prefers_verified_group_evidence_phrase():
    quote = "upstream connection refused (address redacted)"
    text = CONTENT.replace(TRACE, "") + "\n" + quote
    group = {"id": "g", "service": "service-a", "count": 1, "examples": [text]}
    finding = {"group_ids": ["g"], "evidence": [{"source_id": "log:g", "quote": quote}]}
    client = FakeTls(rows=[{"time": str(TIME), "service": "service-a", "content": text}])
    locator = enrich_finding_locators([finding], [group], COVERAGE, client=client)[finding_key(finding)]
    assert locator["term"] == "upstream connection refused"
    assert locator["status"] == "verified"


def test_unobserved_evidence_phrase_is_not_used_for_query():
    text = CONTENT.replace(TRACE, "")
    group = {"id": "g", "service": "service-a", "count": 1, "examples": [text]}
    finding = {"group_ids": ["g"], "evidence": [{"source_id": "log:g", "quote": "invented failure"}]}
    client = FakeTls(rows=[{"time": str(TIME), "service": "service-a", "content": text}])
    locator = enrich_finding_locators([finding], [group], COVERAGE, client=client)[finding_key(finding)]
    assert "invented failure" not in locator["query"]


def test_missing_cached_sampletime_is_recovered_from_actual_result():
    finding, groups = finding_and_groups()
    groups[0]["samples"][0]["time_ms"] = None
    locator = enrich_finding_locators([finding], groups, COVERAGE, client=FakeTls())[finding_key(finding)]
    assert locator["time_ms"] == TIME
