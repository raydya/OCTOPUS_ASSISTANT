import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from scripts import tls_report_evidence as evidence

START = datetime(2026, 10, 20, 8, tzinfo=UTC)
END = datetime(2026, 10, 21, 8, tzinfo=UTC)


class Grafana:
    base_url = "https://grafana.example.test"

    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or [], fail, []

    def query(self, query, end):
        self.calls.append((query, end))
        if self.fail:
            raise evidence.EvidenceUnavailable("grafana_query_failed")
        return self.rows


class Crane:
    def releases(self, name, start, end):
        return {"status": "available", "releases": []}


def test_metrics_are_exact_service_and_cluster_scoped_and_absent_is_not_zero():
    grafana = Grafana([
        {"metric": {"destination_app": "account-service"}, "value": [1, "12"]},
        {"metric": {"destination_app": "jdx-account-service"}, "value": [1, "999"]},
        {"metric": {"container": "account-service"}, "value": [1, "NaN"]},
    ])
    result = evidence.collect_evidence(["account-service", "empty-service"], START, END, grafana=grafana, crane=Crane())
    metrics = result["services"]["account-service"]["metrics"]
    assert metrics["requests"] == {"status": "available", "samples": [{"value": 12.0}]}
    assert metrics["pod_restarts"]["status"] == "no_data"
    assert result["services"]["empty-service"]["metrics"]["requests"]["samples"] == []
    assert all('cluster="pro-jdcloud-sh"' in query and "[86400s]" in query for query, _ in grafana.calls)
    assert all(end == END for _, end in grafana.calls)


def test_failed_source_is_explicit_and_does_not_prevent_other_evidence():
    result = evidence.collect_evidence(["account-service"], START, END, grafana=Grafana(fail=True), crane=Crane())
    assert result["status"] == "partial"
    assert result["services"]["account-service"]["metrics"]["requests"]["status"] == "unavailable"
    assert result["services"]["account-service"]["releases"]["status"] == "available"


def test_crane_postfilters_fuzzy_names_cluster_and_exact_deployment_window(monkeypatch):
    def row(name="account-service", cluster="prod-jd-cloud", time="2026-10-21 12:00:00"):
        return {"AppName": name, "Cluster": cluster, "DeployedAt": time,
                "ID": "23", "ReleaseStatus": "Succeeded", "CommitID": "abc", "Creator": "private-person"}

    rows = [row(), row(name="jdx-account-service"), row(cluster="dev"),
            row(time="2026-10-20 15:59:59"), row(time="2026-10-21 16:00:00")]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    monkeypatch.setattr(evidence.subprocess, "run", run)
    result = evidence.CraneEvidence().releases("account-service", START, END)
    assert len(result["releases"]) == 1
    assert result["releases"][0]["deployed_at"] == "2026-10-21T12:00:00+08:00"
    assert "private-person" not in json.dumps(result)
    assert calls[0][:4] == ["cockpit", "deploy", "release", "list"]
    assert "不能据此认定" in result["interpretation"]


def test_crane_truncation_cannot_claim_complete_coverage(monkeypatch):
    monkeypatch.setattr(evidence.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps([{}] * evidence.RELEASE_LIMIT)))
    result = evidence.CraneEvidence().releases("account-service", START, END)
    assert result["status"] == "partial"


def test_crane_error_does_not_leak_raw_output(monkeypatch):
    monkeypatch.setattr(evidence.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="secret-user-token", stderr="private-stack"))
    with pytest.raises(evidence.EvidenceUnavailable, match="^crane_release_query_failed$"):
        evidence.CraneEvidence().releases("account-service", START, END)


def test_crane_exact_no_releases_sentinel_is_supported(monkeypatch):
    monkeypatch.setattr(evidence.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"message":"No releases found."}'))
    result = evidence.CraneEvidence().releases("account-service", START, END)
    assert result["status"] == "available"
    assert result["releases"] == []


def test_grafana_reuses_only_its_config_credential_without_forwarding_redirects(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.grafana.env]\nGRAFANA_URL="https://grafana.example.test"\nGRAFANA_SERVICE_ACCOUNT_TOKEN="private-existing-token"\n')

    def handler(request):
        assert request.url.host == "grafana.example.test"
        assert request.headers["Authorization"] == "Bearer private-existing-token"
        assert request.url.params["time"] == str(END.timestamp())
        return httpx.Response(302, headers={"Location": "https://another.example.test"})

    client = evidence.GrafanaEvidence(config, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(evidence.EvidenceUnavailable, match="^grafana_query_failed$"):
            client.query("up", END)
    finally:
        client.close()


@pytest.mark.parametrize("start,end,names", [
    ("2026-10-20T08:00:00", END, ["account-service"]),
    (START, START, ["account-service"]),
    (START, END, ['bad"} or up']),
    (START, END, [f"service-{index}" for index in range(13)]),
])
def test_rejects_ambiguous_window_or_unbounded_query_input(start, end, names):
    with pytest.raises(ValueError):
        evidence.collect_evidence(names, start, end, grafana=Grafana(), crane=Crane())


def test_missing_grafana_config_preserves_crane_results(monkeypatch):
    def unavailable():
        raise evidence.EvidenceUnavailable("grafana_credentials_unavailable")

    monkeypatch.setattr(evidence, "GrafanaEvidence", unavailable)
    result = evidence.collect_evidence(["account-service"], START, END, crane=Crane())
    assert result["status"] == "partial"
    assert result["services"]["account-service"]["metrics"]["requests"]["reason"] == "grafana_credentials_unavailable"
