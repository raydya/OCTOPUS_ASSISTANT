"""Collect, diagnose, and publish one dated platform TLS report per day."""

import argparse
import fcntl
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.tls_report_analysis import analyze, source_payload, validate_analysis  # noqa: E402
from scripts.tls_report_evidence import collect_evidence  # noqa: E402
from scripts.tls_report_notify import notify_report  # noqa: E402
from scripts.tls_report_publisher import publish_report  # noqa: E402
from scripts.tls_report_samples import enrich_finding_locators, finding_key, samples_for_group  # noqa: E402
from scripts.tls_report_source import collect_report  # noqa: E402
from scripts.update_platform_services import Cockpit, atomic_write, update_inventory  # noqa: E402

RUNTIME = ROOT / ".data" / "tls-error-reports"
ZONE = ZoneInfo("Asia/Shanghai")


def report_window(day=None, *, now=None):
    now = now or datetime.now(ZONE)
    if now.tzinfo is None:
        raise ValueError("Current time requires a timezone")
    local = now.astimezone(ZONE)
    end = datetime.combine(date.fromisoformat(day) if day else local.date(),
                           datetime.min.time(), ZONE).replace(hour=16)
    if day is None and end > local:
        end -= timedelta(days=1)
    if end > local:
        raise ValueError("Report window has not completed")
    return end - timedelta(days=1), end


def inventory(cockpit="cockpit"):
    path = ROOT / ".data" / "platform-services" / "snapshot.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    stamp = datetime.fromisoformat(current["updated_at"]) if current.get("updated_at") else None
    if stamp is None or stamp.astimezone(ZONE).date() != datetime.now(ZONE).date():
        update_inventory(Cockpit(cockpit))
        current = json.loads(path.read_text())
    if current.get("department_id") != 3 or current.get("department_slug") != "platform" or not current.get("services"):
        raise ValueError("Platform ownership inventory is unavailable")
    return current


def normalize_summary(summary):
    summary = re.sub(r"([A-Za-z_$][\w.$]*)@(?:<n>|[0-9a-f])+", r"\1@<object>", summary)
    if re.match(r"^\s*(?:at [\w.$]+\(|\.\.\. <n> (?:common )?frames omitted)", summary):
        return "堆栈续行（不单独代表一个异常事件）"
    return summary


def model_groups(raw_groups):
    """Merge identical mechanisms across services, retaining exact SQL counts."""
    merged = {}
    for row in raw_groups:
        key = (row.get("logger", ""), row.get("exception", ""), normalize_summary(row.get("summary", "")))
        identifier = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:20]
        item = merged.setdefault(identifier, {
            "id": identifier, "count": 0, "services": [], "service_counts": {},
            "logger": key[0], "exception": key[1], "summary": key[2], "examples": [],
            "samples": [],
        })
        item["count"] += row["count"]
        name = row["service"]
        item["service_counts"][name] = item["service_counts"].get(name, 0) + row["count"]
        if name not in item["services"]:
            item["services"].append(name)
        for example in row.get("examples", []):
            if example not in item["examples"] and len(item["examples"]) < 2:
                item["examples"].append(example[:1800])
        for sample in samples_for_group(row):
            if sample not in item["samples"] and len(item["samples"]) < 4:
                item["samples"].append(sample)
    return sorted(merged.values(), key=lambda row: (-row["count"], row["id"]))


def compact_payload(groups, evidence, coverage):
    # Locators are selected and verified by code, not invented by the model.
    # Keep identifiers outside content redaction and the analysis input budget.
    payload = source_payload([{key: value for key, value in row.items() if key != "samples"}
                              for row in groups], evidence, scope=coverage)
    # The canonical log evidence already lives in sources. Do not send each
    # full stack trace twice just to list the groups that must be classified.
    payload["groups"] = [
        {key: row[key] for key in ("id", "count", "summary", "services") if key in row}
        for row in payload["groups"]
    ]
    return payload


def prepare_payload(collected, evidence):
    groups = model_groups(collected["groups"])
    selected = []
    for group in groups:
        candidate = compact_payload([*selected, group], evidence, collected["coverage"])
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > 118000 or len(selected) >= 80:
            break
        selected.append(group)
    if groups and not selected:
        raise ValueError("Report evidence exceeds the analysis input limit")
    payload = compact_payload(selected, evidence, collected["coverage"])
    return payload, {"template_groups": len(groups), "analyzed_groups": len(selected),
                     "analyzed_errors": sum(group["count"] for group in selected)}


def escape(value, *, quote=True):
    # XML 1.0 rejects control characters even after HTML entity escaping.
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(value))
    return html_escape(clean, quote=quote)


def local_time(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value).astimezone(ZONE)
    return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, ZONE)


def render_locator(locator):
    if not locator or locator.get("status") != "verified":
        return "<p>TLS 定位查询暂未核验，不能据此认定该类错误没有日志。</p>"
    begin, end = local_time(locator["window_start"]), local_time(locator["window_end"])
    sample_time = local_time(locator["time_ms"]) if locator.get("time_ms") else None
    detail = f"TLS 查询：{locator['service']}；时间范围（北京时间）：{begin:%Y-%m-%d %H:%M:%S}—{end:%Y-%m-%d %H:%M:%S}。"
    if sample_time:
        detail += f"样例时间：{sample_time:%m-%d %H:%M:%S}。"
    if locator.get("swtid"):
        detail += f"样例 swtid：{locator['swtid']}。"
    return (f"<p>{escape(detail)}</p>\n"
            f'<pre lang="sql"><code>{escape(locator["query"])}</code></pre>')


def render_report(day, collected, evidence, result, analysis_coverage, locators=None):
    coverage = collected["coverage"]
    start = datetime.fromtimestamp(coverage["start"] / 1000 if coverage["start"] > 10**11 else coverage["start"], ZONE)
    end = datetime.fromtimestamp(coverage["end"] / 1000 if coverage["end"] > 10**11 else coverage["end"], ZONE)
    total, analyzed = coverage["total_errors"], analysis_coverage["analyzed_errors"]
    paragraphs = [
        f"<p>窗口：{start:%m-%d %H:%M}—{end:%m-%d %H:%M}（北京时间，24小时）。"
        f"{coverage['matched_services']} 个平台服务，{total:,} 条错误。</p>",
    ]
    if locators:
        paragraphs.append("<p>日志定位：进入 TLS 上海区域 → pro-jd-sh-service → error，"
                          "先选择各类别注明的时间范围，再粘贴对应查询语句。</p>")
    if not total:
        paragraphs.append("<p><b>本窗口未检出平台研发服务错误日志。</b></p>")
    elif result:
        paragraphs.append(f"<p><b>{escape(result['summary'])}</b></p>")
        groups = {row["id"]: row for row in model_groups(collected["groups"])}
        paragraphs.append('<h1 seq="auto">归并结论与处置</h1>')
        for finding in result["findings"]:
            rows = [groups[key] for key in finding["group_ids"]]
            count = sum(row["count"] for row in rows)
            service_counts = defaultdict(int)
            for row in rows:
                for name, number in row["service_counts"].items():
                    service_counts[name] += number
            ordered = sorted(service_counts, key=lambda name: (-service_counts[name], name))
            names = "、".join(ordered[:5]) + (f" 等 {len(ordered)} 个服务" if len(ordered) > 5 else "")
            certainty = "已确认" if finding["cause_status"] == "confirmed" else "待核实"
            evidence_text = "；".join(citation["quote"] for citation in finding["evidence"][:2])[:300]
            paragraphs.append(
                f"<p><b>{escape(finding['priority'] + ' · ' + finding['title'])}｜{count:,} 条</b><br/>"
                f"服务：{escape(names)}<br/>"
                f"直接原因：{escape(finding['direct_cause'])}<br/>"
                f"根因（{certainty}）：{escape(finding['root_cause'])}<br/>"
                f"处置：{escape('；'.join(finding['actions']))}<br/>"
                f"证据：{escape(evidence_text)}</p>"
            )
            if locators is not None:
                paragraphs.append(render_locator(locators.get(finding_key(finding))))
    else:
        paragraphs.append("<p><b>日志统计已完成，自动归因未完成；本次不提供未经核验的根因。</b></p>")
    if total:
        leading = sorted(coverage["service_counts"].items(), key=lambda pair: (-pair[1], pair[0]))[:8]
        paragraphs.extend(['<h1 seq="auto">错误量最高的服务</h1>',
                           "<p>" + escape("；".join(f"{name} {count:,}条" for name, count in leading)) + "。</p>"])
    limitations = []
    if not coverage["complete"]:
        limitations.append(f"TLS 分组未完整覆盖：{coverage['ungrouped_errors']:,} 条尚未归并。")
    if total and analyzed < coverage["grouped_errors"]:
        limitations.append(f"归因选取高频 {analysis_coverage['analyzed_groups']} 个模板，覆盖 {analyzed:,} 条（{analyzed / total:.1%}）；其余日志仅统计，未逐项归因。")
    if evidence.get("status") == "partial":
        limitations.append("部分 Grafana/Crane 查询不可用，未据缺失指标推断根因。")
    limitations.append("条数为TLS日志记录数，包含堆栈续行，不等同独立异常或失败请求数；原因来自代表样本。")
    if coverage.get("sample_index_max_text_length"):
        limitations.append(f"正文索引最多保留 {coverage['sample_index_max_text_length']} 字节，深层堆栈可能缺失。")
    selected_services = len(evidence.get("services", {}))
    limitations.append(f"Grafana/Crane 交叉核验错误量最高的 {selected_services} 个服务；无指标样本不等于零异常，发布时间接近不证明因果。")
    paragraphs.extend(['<h1 seq="auto">核验范围</h1>', "<p>" + escape(" ".join(limitations)) + "</p>"])
    topic_id = coverage["queried_topics"][0]["id"]
    paragraphs.append(f"<p>来源：TLS cn-shanghai / pro-jd-sh-service / error；Topic ID：{escape(topic_id)}。服务归属：Crane 平台研发部。</p>")
    if evidence.get("grafana_dashboard"):
        paragraphs.append(f'<p><a href="{escape(evidence["grafana_dashboard"], quote=True)}">Grafana 服务核验看板</a></p>')
    return "\n".join(paragraphs)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(day=None, *, collect_only=False, cockpit="cockpit", republish=False):
    start, end = report_window(day)
    day = end.date().isoformat()
    folder = RUNTIME / "days" / day
    folder.mkdir(parents=True, exist_ok=True)
    done = folder / "published.json"
    if done.exists() and (not republish or collect_only):
        return {**json.loads(done.read_text()), "status": "already_published"}
    source_file, evidence_file = folder / "collected.json", folder / "evidence.json"
    if source_file.exists():
        collected = json.loads(source_file.read_text())
    else:
        ownership = inventory(cockpit)
        collected = collect_report(start, end, ownership["services"])
        collected["ownership_updated_at"] = ownership["updated_at"]
        save_json(source_file, collected)
    if evidence_file.exists():
        evidence = json.loads(evidence_file.read_text())
    else:
        services = [name for name, _ in sorted(collected["coverage"]["service_counts"].items(), key=lambda pair: (-pair[1], pair[0]))[:12]]
        evidence = collect_evidence(services, start, end)
        save_json(evidence_file, evidence)
    payload, analysis_coverage = prepare_payload(collected, evidence)
    save_json(folder / "analysis-input.json", payload)
    save_json(folder / "analysis-coverage.json", analysis_coverage)
    if collect_only:
        return {"status": "collected", "report_date": day, "errors": collected["coverage"]["total_errors"],
                "services": collected["coverage"]["matched_services"], **analysis_coverage}
    analysis_file = folder / "analysis.json"
    result = None
    if payload["groups"]:
        if analysis_file.exists():
            result = validate_analysis(json.loads(analysis_file.read_text()), payload)
        else:
            print(json.dumps({"phase": "analyzing", "report_date": day, **analysis_coverage}), flush=True)
            result = analyze(payload)
            save_json(analysis_file, result)
    locators = {}
    if result:
        locator_file = folder / "locators.json"
        if locator_file.exists():
            locators = json.loads(locator_file.read_text())
        keys = {finding_key(finding) for finding in result["findings"]}
        if set(locators) != keys or any(row.get("status") != "verified" for row in locators.values()):
            locators = enrich_finding_locators(result["findings"], model_groups(collected["groups"]), collected["coverage"])
            save_json(locator_file, locators)
    body = render_report(day, collected, evidence, result, analysis_coverage, locators)
    atomic_write(folder / "report.xml", body)
    publication = publish_report(day, body)
    record = {"report_date": day, "published_at": datetime.now(ZONE).isoformat(timespec="seconds"),
              "errors": collected["coverage"]["total_errors"], "services": collected["coverage"]["matched_services"],
              "categories": len(result["findings"]) if result else 0,
              "content_hash": hashlib.sha256(body.encode()).hexdigest(), **publication}
    save_json(done, record)
    return {**record, "status": "published"}


def execute(day=None, *, collect_only=False, cockpit="cockpit", republish=False):
    # Invalid or unfinished date selections are not completed report attempts.
    _, end = report_window(day)
    report_date = end.date().isoformat()
    try:
        result = run(report_date, collect_only=collect_only, cockpit=cockpit, republish=republish)
    except Exception as error:
        result = {"status": "failed", "report_date": report_date,
                  "error_type": type(error).__name__,
                  "attempted_at": datetime.now(ZONE).isoformat(timespec="seconds")}
    if not collect_only:
        try:
            result["notification"] = notify_report(report_date, result)
        except Exception as error:
            # A notification outage never changes the document's success state.
            result["notification"] = {"status": "failed", "error_type": type(error).__name__}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Completed report day, YYYY-MM-DD; defaults to the latest 16:00 window")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--republish", action="store_true", help="Update the existing dated report from cached, verified evidence")
    parser.add_argument("--cockpit", default="cockpit")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "job.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return 0
        try:
            result = execute(args.date, collect_only=args.collect_only, cockpit=args.cockpit, republish=args.republish)
        except Exception as error:
            # Log the failure class, never credentials, raw logs, or CLI stderr.
            result = {"status": "failed", "error_type": type(error).__name__,
                      "attempted_at": datetime.now(ZONE).isoformat(timespec="seconds")}
            save_json(RUNTIME / "last-run.json", result)
            print(json.dumps(result, ensure_ascii=False))
            return 1
        save_json(RUNTIME / "last-run.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] == "failed" or result.get("notification", {}).get("status") in {"failed", "unknown"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
