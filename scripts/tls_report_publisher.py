"""Publish one owned TLS report per day through the user's authenticated lark-cli.

All remote mutations are preceded by a persisted intent. An uncertain create is
recovered by listing the selected parent; it is never blindly repeated. Updates
replace only the report's marked block range and preserve surrounding user text.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PARENT_URL = "https://atrenew.feishu.cn/wiki/Z7FUwrWH3ioGtPk3o41cUqojnvg"
STATE_DIR = ROOT / ".data" / "tls-error-reports" / "publisher"
PRESENTATION = {
    "audience": "平台研发团队负责人和值班工程师",
    "reader_task": "快速辨认过去24小时错误的集中成因并决定处置",
    "genre_contract": "report.data_report", "adapter": None,
    "presentation_mode": "formal",
    "visual_plan": {
        "reason": "用精简段落和表格归并错误，给出证据、原因可信度和处置，不使用装饰性图片。",
        "blocks": [],
    },
}


class PublishError(RuntimeError):
    """A safe error which excludes raw CLI output and authentication material."""


class LarkCLI:
    def __init__(self, binary="lark-cli"):
        self.binary = binary

    def run(self, args, *, cwd=ROOT, content=None):
        environment = dict(os.environ)
        environment.update(LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
                           LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        try:
            result = subprocess.run(
                [self.binary, *args, "--as", "user", "--format", "json"],
                input=content, capture_output=True, text=True, cwd=cwd,
                timeout=180, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PublishError("飞书命令未完成；已保留发布状态，不会盲目重复创建。") from None
        try:
            envelope = json.loads(result.stdout if result.returncode == 0 else result.stderr)
        except (ValueError, TypeError):
            raise PublishError("飞书命令未返回可核验 JSON；已保留发布状态。") from None
        if result.returncode != 0 or envelope.get("ok") is not True:
            error = envelope.get("error", {})
            kind = str(error.get("type", "unknown"))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", kind):
                kind = "unknown"
            raise PublishError(f"飞书命令失败（{kind}），请检查登录态、目录权限和 CLI 状态。")
        if envelope.get("identity") not in {None, "user"}:
            raise PublishError("飞书命令身份不符，停止发布。")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise PublishError("飞书命令返回结构不完整。")
        return data


def _xml(content):
    if not isinstance(content, str) or "<!" in content or "<?" in content:
        raise PublishError("日报 XML 含不支持的声明。")
    try:
        return ET.fromstring(f"<root>{content}</root>")
    except ET.ParseError:
        raise PublishError("日报 XML 结构不完整。") from None


def _digest(elements):
    text = "".join("".join(node.itertext()) for node in elements)
    links = sorted(node.get("href", "") for parent in elements
                   for node in parent.iter() if node.tag == "a")
    # Tokenized user additions can contain no text. Include them so an inserted
    # image/file/embedded object cannot silently disappear during a later update.
    resources = [
        [node.tag, sorted((key, value) for key, value in node.attrib.items() if key != "id")]
        for parent in elements for node in parent.iter()
        if node.tag in {"img", "source", "whiteboard", "sheet", "bitable", "cite",
                        "synced_reference", "synced_source", "html5-block", "chat_card"}
    ]
    canonical = json.dumps([re.sub(r"\s+", "", text), links, resources], ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def report_title(report_date):
    day = date.fromisoformat(str(report_date)).isoformat()
    return f"{day} 平台研发错误日志日报"


def _markers(day):
    return (
        f"统计日期：{day}；范围：上海 pro-jd-sh-service / 平台研发团队。",
        f"来源：OCTOPUS 每日错误日志分析 · {day}",
    )


def _document_xml(day, body):
    nodes = _xml(body)
    if not list(nodes) or any(node.tag == "title" for node in nodes.iter()):
        raise PublishError("请提供非空 XML 正文，不包含 title。")
    start, end = _markers(day)
    content = f"<p>{escape(start)}</p>\n{body}\n<p>{escape(end)}</p>"
    return f"<title>{escape(report_title(day))}</title>\n{content}", content


def _owned_region(content, day):
    nodes = list(_xml(content))
    start, end = _markers(day)
    beginnings = [i for i, node in enumerate(nodes)
                  if node.tag == "p" and "".join(node.itertext()) == start]
    endings = [i for i, node in enumerate(nodes)
               if node.tag == "p" and "".join(node.itertext()) == end]
    if len(beginnings) != 1 or len(endings) != 1 or beginnings[0] >= endings[0]:
        raise PublishError("同名文档缺少唯一日报归属标记，未修改。")
    selected = nodes[beginnings[0]:endings[0] + 1]
    first, last = selected[0].get("id"), selected[-1].get("id")
    if not first or not last:
        raise PublishError("日报正文缺少可核验的块 ID，未修改。")
    return selected, first, last


def _save(path, state):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _draft(client, xml, directory):
    result = client.run([
        "docs", "+script", "--command", "init-draft", "--presentation-decision",
        json.dumps(PRESENTATION, ensure_ascii=False),
    ], cwd=directory)
    cwd = Path(result.get("cwd", "")).resolve()
    draft = (cwd / result.get("draft_path", "")).resolve()
    if cwd != directory.resolve() or not draft.is_relative_to(cwd) or draft == cwd:
        raise PublishError("飞书草稿路径不在任务工作区内。")
    draft.write_text(xml, encoding="utf-8")
    draft.chmod(0o600)
    relative = "@./" + str(draft.relative_to(cwd))
    parsed = client.run(["docs", "+script", "--command", "parse", "--content", relative],
                        cwd=cwd)
    if parsed.get("assessment", {}).get("status") != "passed":
        raise PublishError("日报草稿校验未通过，未写入飞书。")
    return cwd, relative


def _children(client, parent):
    rows, cursor, seen = [], "", set()
    for _ in range(1000):
        args = ["wiki", "+node-list", "--space-id", parent["space_id"],
                "--parent-node-token", parent["node_token"], "--page-size", "50"]
        if cursor:
            args += ["--page-token", cursor]
        response = client.run(args)
        nodes = response.get("nodes")
        if not isinstance(nodes, list):
            raise PublishError("飞书目录列表不完整，停止创建。")
        for node in nodes:
            if not isinstance(node, dict) or not node.get("node_token"):
                raise PublishError("飞书目录节点结构不完整，停止创建。")
            if node["node_token"] in seen:
                raise PublishError("飞书目录分页重复，停止创建。")
            seen.add(node["node_token"])
            rows.append(node)
        if response.get("has_more") is False:
            return rows
        next_cursor = response.get("page_token")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise PublishError("飞书目录分页不完整，停止创建。")
        cursor = next_cursor
    raise PublishError("飞书目录超过分页保护上限，停止创建。")


def _fetch(client, document_id):
    data = client.run(["docs", "+fetch", "--doc", document_id, "--detail", "full"])
    document = data.get("document", {})
    if not isinstance(document.get("content"), str) or "revision_id" not in document:
        raise PublishError("飞书日报回读不完整。")
    return document


def publish_report(report_date, xml_body, *, parent_url=PARENT_URL, state_dir=STATE_DIR,
                   client=None):
    """Return {url, document_id, node_token, status}; no shell interpolation.

    ``xml_body`` contains documented DocxXML blocks, without <title>. Headings
    should use seq="auto". Caller must escape log-derived text before rendering.
    A repeated date updates only this job's marked range after a revision check.
    """
    day = date.fromisoformat(str(report_date)).isoformat()
    xml, region_xml = _document_xml(day, xml_body)
    expected_hash = _digest(list(_xml(region_xml)))
    client = client or LarkCLI()
    directory = Path(state_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_key = hashlib.sha256(parent_url.encode()).hexdigest()[:16]
    state_path = directory / f"{parent_key}-{day}.json"
    with (directory / f"{parent_key}-{day}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError("同一天的日报正在发布，跳过重复执行。") from None
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (OSError, ValueError):
            raise PublishError("日报持久化状态无法读取，未执行发布。") from None
        if not isinstance(state, dict):
            raise PublishError("日报持久化状态结构无效，未执行发布。")
        return _publish(client, day, xml, region_xml, expected_hash, parent_url,
                        directory, state_path, state)


def _publish(client, day, xml, region_xml, expected_hash, parent_url,
             directory, state_path, state):
    parent = client.run(["wiki", "+node-get", "--node-token", parent_url])
    if not all(parent.get(key) for key in ("space_id", "node_token")):
        raise PublishError("日报父目录无法核实。")
    if state and (state.get("parent_node_token") != parent["node_token"]
                  or state.get("report_date") != day):
        raise PublishError("日报持久化状态与目标目录不一致，停止发布。")
    nodes = _children(client, parent)
    candidates = [node for node in nodes if node.get("title") == report_title(day)]
    if state.get("document_id"):
        candidates = [node for node in nodes if node.get("obj_token") == state["document_id"]]
        if len(candidates) != 1:
            raise PublishError("已发布日报不在原目录，未重新创建或移动文档。")
    elif len(candidates) > 1:
        raise PublishError("同日期日报有多个候选，未创建或修改文档。")
    if candidates:
        node = candidates[0]
        if node.get("obj_type") != "docx" or not node.get("obj_token"):
            raise PublishError("同名节点不是可核验的日报文档，未修改。")
        document = _fetch(client, node["obj_token"])
        region, first, last = _owned_region(document["content"], day)
        remote_hash = _digest(region)
        if remote_hash != expected_hash:
            known_hashes = {state.get("content_hash"), state.get("attempted_hash")} - {None}
            if remote_hash not in known_hashes:
                raise PublishError("日报正文有未核验的修改，保留现有内容。")
            _draft(client, xml, directory)
            state.update(phase="updating", attempted_hash=expected_hash,
                         document_id=node["obj_token"], node_token=node["node_token"])
            _save(state_path, state)
            try:
                result = client.run([
                    "docs", "+update", "--doc", node["obj_token"], "--command", "block_replace",
                    "--start-block-id", first, "--end-block-id", last,
                    "--revision-id", str(document["revision_id"]),
                    "--doc-format", "xml", "--content", "-",
                ], content=region_xml)
                if result.get("result") not in {None, "success"} or result.get("warnings"):
                    raise PublishError("日报更新存在局部失败，保留状态等待回查。")
            except PublishError:
                # A lost response may still mean success. Only read back here.
                verified = _fetch(client, node["obj_token"])
                updated, _, _ = _owned_region(verified["content"], day)
                if _digest(updated) != expected_hash:
                    raise
            status = "updated"
        else:
            status = "unchanged"
    else:
        if state:
            raise PublishError("上次创建结果尚不能从目录确认；不会再次创建同日期日报。")
        cwd, relative = _draft(client, xml, directory)
        state.update(report_date=day, parent_node_token=parent["node_token"],
                     phase="creating", attempted_hash=expected_hash)
        _save(state_path, state)
        try:
            result = client.run(["docs", "+create", "--parent-token", parent["node_token"],
                                 "--doc-format", "xml", "--content", relative], cwd=cwd)
            created_id = result.get("document", {}).get("document_id")
            if created_id:
                state["document_id"] = created_id
                _save(state_path, state)
        except PublishError:
            pass  # Discover a successful but unacknowledged create below.
        nodes = _children(client, parent)
        candidates = [node for node in nodes if node.get("title") == report_title(day)
                      and (not state.get("document_id")
                           or node.get("obj_token") == state["document_id"])]
        if len(candidates) != 1:
            raise PublishError("创建结果尚不能从目录确认；已保存状态，不会重复创建。")
        node, status = candidates[0], "created"
        if node.get("obj_type") != "docx" or not node.get("obj_token"):
            raise PublishError("创建结果节点类型无法核验。")
    verified = _fetch(client, node["obj_token"])
    region, _, _ = _owned_region(verified["content"], day)
    if _digest(region) != expected_hash:
        raise PublishError("日报回读内容与预期不一致；未标记发布成功。")
    state.update(report_date=day, parent_node_token=parent["node_token"], phase="published",
                 document_id=node["obj_token"], node_token=node["node_token"],
                 content_hash=expected_hash, attempted_hash=expected_hash)
    _save(state_path, state)
    origin = urlsplit(parent_url)
    url = f"{origin.scheme}://{origin.netloc}/wiki/{node['node_token']}"
    return {"url": url, "document_id": node["obj_token"],
            "node_token": node["node_token"], "status": status}
