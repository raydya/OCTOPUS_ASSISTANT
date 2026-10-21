"""Evidence-derived TLS log locators, independent of model-generated output."""

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TRACE_ID = re.compile(r"[0-9a-fA-F]{32}")
LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?)")
HEADER_TRACE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?"
    r"(?:Z|[+-]\d{2}:\d{2})?\s+(?:\[[^\]\r\n]*\]\s+)?)?"
    r"ERROR\s*\[\s*([0-9a-fA-F]{32})\s*,"
)
EXPLICIT_TRACE = re.compile(r"(?i)(?:^|[\s,{])[\"']?swtid[\"']?\s*[:=]\s*[\"']?([0-9a-f]{32})(?=$|[\s\"',;}\]])")
EXCEPTION_NAME = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_.$]*(?:Exception|Error))\b")
ZONE = ZoneInfo("Asia/Shanghai")


def extract_swtid(raw):
    """Only explicit swtid fields or the first ID in the ERROR header qualify."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).lower() == "swtid" and isinstance(value, str) and TRACE_ID.fullmatch(value):
                return value
        text = raw.get("__content__", raw.get("content", ""))
    else:
        text = raw
    if not isinstance(text, str):
        return None
    explicit = EXPLICIT_TRACE.search(text)
    if explicit:
        return explicit[1]
    match = HEADER_TRACE.match(text)
    return match[1] if match else None


def sample_time(raw, fallback=None):
    if isinstance(raw, dict):
        value = raw.get("__time__", raw.get("time", fallback))
        text = raw.get("__content__", raw.get("content", ""))
    else:
        value, text = fallback, raw
    if not isinstance(value, bool) and re.fullmatch(r"\d{13}", str(value)):
        return int(value)
    match = LOG_TIMESTAMP.match(text) if isinstance(text, str) else None
    if match:
        try:
            parsed = datetime.fromisoformat(match[1].replace(",", ".").replace("Z", "+00:00"))
            return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=ZONE)).timestamp() * 1000)
        except ValueError:
            pass
    return None


def _phrase(text):
    # A real exception class is stable, non-personal and copyable. Prefer the
    # deepest visible cause; truncated stacks are not grounds to invent one.
    causes = re.findall(r"Caused by:\s*([A-Za-z_$][A-Za-z0-9_.$]*(?:Exception|Error))", text)
    if causes:
        return causes[-1]
    if "invalid entry size" in text:
        return "invalid entry size"
    names = EXCEPTION_NAME.findall(text)
    if names:
        return names[-1]
    first = text.splitlines()[0] if text else ""
    message = first.split(" - ", 1)[-1] if " - " in first else ""
    # Avoid copying dynamic JSON/request payloads or redacted placeholders into
    # a query, where they would either disclose data or never match real logs.
    prefix = re.split(r"[{}\[\]<>:=]", message, maxsplit=1)[0].strip()
    return prefix[:60] if len(prefix) >= 6 and not re.search(r"\d{5,}", prefix) else ""


def extract_sample(raw, service, *, sample_time_ms=None, source="cached_log"):
    if not isinstance(service, str) or not service:
        return None
    if isinstance(raw, dict):
        if raw.get("topic", service) != service:
            return None
        text = raw.get("__content__", raw.get("content", ""))
    else:
        text = raw
    if not isinstance(text, str) or not text:
        return None
    swtid, phrase = extract_swtid(raw), _phrase(text)
    if not swtid and not phrase:
        return None
    record = {"service": service, "time_ms": sample_time(raw, sample_time_ms),
              "source": source, "verified": source == "tls_log", "phrase": phrase}
    if swtid:
        record["swtid"] = swtid
    return record


def samples_for_group(group):
    """Preserve future source metadata and recover usable historical examples."""
    services = group.get("services") or [group.get("service")]
    result = []
    for sample in group.get("samples", []):
        if (isinstance(sample, dict) and sample.get("service") in services
                and (TRACE_ID.fullmatch(str(sample.get("swtid", ""))) or sample.get("phrase"))):
            if sample not in result:
                result.append(dict(sample))
    if len(services) == 1 and services[0]:
        for text in group.get("examples", []):
            sample = extract_sample(text, services[0])
            if sample and not any((s.get("swtid"), s.get("time_ms"), s.get("phrase")) ==
                                  (sample.get("swtid"), sample.get("time_ms"), sample.get("phrase"))
                                  for s in result):
                result.append(sample)
    return result


def finding_key(finding):
    return hashlib.sha256(json.dumps(sorted(finding["group_ids"])).encode()).hexdigest()[:20]


def _candidates(finding, groups):
    mapping = groups if isinstance(groups, dict) else {group["id"]: group for group in groups}
    selected = [mapping[key] for key in finding["group_ids"] if key in mapping]
    for group in sorted(selected, key=lambda item: -item.get("count", 0)):
        evidence_phrase = ""
        for citation in finding.get("evidence", []):
            quote = citation.get("quote", "")
            if (citation.get("source_id") != f"log:{group['id']}" or not isinstance(quote, str)
                    or not any(quote in text for text in group.get("examples", []))):
                continue
            phrase = re.split(r"[\n\r(\[{:＝=]", quote, maxsplit=1)[0].strip()
            if (6 <= len(phrase) <= 100 and not re.search(r"\d{5,}|https?://|@|%|_|\\|<|>", phrase)
                    and not re.search(r"(?i)password|secret|token|authorization", phrase)):
                evidence_phrase = phrase
                break
        for sample in sorted(samples_for_group(group), key=lambda s: (not bool(s.get("swtid")), s.get("time_ms") is None)):
            if not sample.get("swtid") and evidence_phrase:
                sample = {**sample, "phrase": evidence_phrase}
            yield {**sample, "group_id": group["id"]}


def finding_sample(finding, groups):
    return next(_candidates(finding, groups), None)


def _quoted(value):
    return "'" + value.replace("'", "''") + "'"


def build_locator(sample, coverage):
    service, swtid = sample["service"], sample.get("swtid")
    swtid = swtid if isinstance(swtid, str) and TRACE_ID.fullmatch(swtid) else None
    term = swtid or sample.get("phrase")
    if not isinstance(term, str) or not term or any(c in term for c in ("%", "_", "\\", "\n", "\r")):
        return None
    start, end = coverage["start"], coverage["end"]
    stamp = sample.get("time_ms")
    if type(stamp) is int and start * 1000 <= stamp < end * 1000:
        start, end = max(start, stamp // 1000 - 60), min(end, stamp // 1000 + 61)
    else:
        stamp = None
    query = ("* | select __time__ as time, topic as service, __content__ as content "
             f"where topic = {_quoted(service)} and __content__ like {_quoted('%' + term + '%')} "
             "order by __time__ limit 100")
    return {"query": query, "service": service, "window_start": start, "window_end": end,
            "time_ms": stamp, "swtid": swtid or None, "term": term,
            "group_id": sample.get("group_id"), "status": "unverified"}


def enrich_finding_locators(findings, groups, coverage, *, client=None, progress=None):
    # Import lazily: the collector imports extraction helpers before calling TLS.
    from scripts.tls_report_source import PROJECT_NAME, REGION, TOPIC_NAME, SourceError, TlsClient

    if coverage.get("project") != PROJECT_NAME or coverage.get("region") != REGION:
        raise ValueError("Unexpected TLS report scope")
    topics = coverage.get("queried_topics", [])
    if len(topics) != 1 or topics[0].get("name") != TOPIC_NAME:
        raise ValueError("Unexpected TLS topic")
    topic_id = topics[0]["id"]
    client = client or TlsClient()
    locators = {}
    for index, finding in enumerate(findings, 1):
        key, seen = finding_key(finding), set()
        locator = {"status": "unavailable", "reason": "未找到可核验的原始日志样例；不生成查询标识。"}
        for sample in _candidates(finding, groups):
            candidate = build_locator(sample, coverage)
            if not candidate:
                continue
            request_key = (candidate["query"], candidate["window_start"], candidate["window_end"])
            if request_key in seen:
                continue
            seen.add(request_key)
            if len(seen) > 3:
                break
            try:
                data = client.call("log.search", {"TopicId": topic_id,
                    "StartTime": candidate["window_start"], "EndTime": candidate["window_end"],
                    "Query": candidate["query"], "MustComplete": True})
            except SourceError:
                locator = {"status": "unavailable", "reason": "TLS 样例查询未完成，未标记为已验证。"}
                continue
            rows = (data.get("AnalysisResult") or {}).get("Data", [])
            if data.get("ResultStatus") != "complete" or not isinstance(rows, list):
                continue
            valid = [row for row in rows if isinstance(row, dict) and row.get("service") == candidate["service"]
                     and candidate["term"] in str(row.get("content", ""))]
            if not valid:
                continue
            actual_time = sample_time({"time": valid[0].get("time")})
            locator = {**candidate, "status": "verified", "validated_match_count": len(valid),
                       "match_count_is_total": False, "time_ms": actual_time or candidate["time_ms"]}
            break
        locators[key] = locator
        if progress:
            progress({"phase": "tls_sample_query", "completed": index, "total": len(findings),
                      "status": locator["status"], "matched": locator.get("validated_match_count", 0)})
    return locators
