import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import tls_report_publisher as publisher

DAY = "2026-10-21"
BODY = '<p>最近24小时：2类错误。</p><h1 seq="auto">处置</h1><p>先检查连接池。</p>'


class FakeLark:
    def __init__(self):
        self.calls = []
        self.nodes = []
        self.documents = {}
        self.create_count = 0
        self.update_count = 0
        self.lose_create_response = False
        self.create_no_effect = False
        self.lose_update_response = False
        self.parse_status = "passed"
        self.sequence = 0

    def run(self, args, *, cwd=None, content=None):
        self.calls.append((list(args), cwd, content))

        def value(flag):
            return args[args.index(flag) + 1]

        if args[:2] == ["wiki", "+node-get"]:
            return {"space_id": "123", "node_token": "parent"}
        if args[:2] == ["wiki", "+node-list"]:
            return {"nodes": list(self.nodes), "has_more": False}
        if args[:2] == ["docs", "+script"]:
            if value("--command") == "parse":
                return {"assessment": {"status": self.parse_status}}
            self.sequence += 1
            folder = Path(cwd) / f"draft_{self.sequence}"
            folder.mkdir()
            return {"cwd": str(cwd), "draft_path": str(folder.relative_to(cwd) / "draft.xml")}
        if args[:2] == ["docs", "+fetch"]:
            return {"document": dict(self.documents[value("--doc")])}
        if args[:2] == ["docs", "+create"]:
            self.create_count += 1
            if self.create_no_effect:
                raise publisher.PublishError("lost response")
            xml = (Path(cwd) / value("--content")[1:]).read_text()
            self.add_document(xml)
            if self.lose_create_response:
                raise publisher.PublishError("lost response")
            return {"document": {"document_id": "document"}, "warnings": []}
        if args[:2] == ["docs", "+update"]:
            self.update_count += 1
            doc = self.documents[value("--doc")]
            assert int(value("--revision-id")) == doc["revision_id"]
            root = ET.fromstring(f'<root>{doc["content"]}</root>')
            items = list(root)
            begin = next(i for i, item in enumerate(items)
                         if item.get("id") == value("--start-block-id"))
            end = next(i for i, item in enumerate(items)
                       if item.get("id") == value("--end-block-id"))
            replacement = self._with_ids(content)
            doc["content"] = "".join(ET.tostring(item, encoding="unicode")
                                      for item in items[:begin]) + replacement + "".join(
                ET.tostring(item, encoding="unicode") for item in items[end + 1:])
            doc["revision_id"] += 1
            if self.lose_update_response:
                raise publisher.PublishError("lost response")
            return {"result": "success", "warnings": []}
        raise AssertionError(args)

    def _with_ids(self, xml):
        root = ET.fromstring(f"<root>{xml}</root>")
        for node in root:
            self.sequence += 1
            node.set("id", f"block_{self.sequence}")
        return "".join(ET.tostring(item, encoding="unicode") for item in root)

    def add_document(self, xml):
        self.nodes.append({"title": publisher.report_title(DAY), "node_token": "node",
                           "obj_token": "document", "obj_type": "docx"})
        self.documents["document"] = {"content": self._with_ids(xml), "revision_id": 1}


@pytest.fixture
def publication(tmp_path):
    client = FakeLark()

    def run(body=BODY):
        return publisher.publish_report(DAY, body, state_dir=tmp_path, client=client)

    return client, run, tmp_path


def test_creates_complete_report_then_identical_run_does_not_write(publication):
    client, run, path = publication
    result = run()
    assert result == {"document_id": "document", "node_token": "node", "status": "created",
                      "url": "https://atrenew.feishu.cn/wiki/node"}
    assert run()["status"] == "unchanged"
    assert client.create_count == 1 and client.update_count == 0
    state = json.loads(next(path.glob("*.json")).read_text())
    assert state["phase"] == "published"
    assert "最近24小时" in client.documents["document"]["content"]
    parse_index = next(i for i, call in enumerate(client.calls)
                       if call[0][:2] == ["docs", "+script"] and "parse" in call[0])
    create_index = next(i for i, call in enumerate(client.calls) if call[0][:2] == ["docs", "+create"])
    assert parse_index < create_index


def test_same_date_replaces_only_owned_blocks_and_keeps_user_notes(publication):
    client, run, _ = publication
    run()
    document = client.documents["document"]
    document["content"] = ('<p id="before">用户前言</p>' + document["content"]
                           + '<p id="after">用户补充结论</p>')
    assert run('<p>最近24小时：3类错误。</p>')["status"] == "updated"
    assert "用户前言" in document["content"] and "用户补充结论" in document["content"]
    assert "3类错误" in document["content"] and "2类错误" not in document["content"]
    assert client.create_count == 1 and client.update_count == 1


def test_manual_edit_inside_owned_range_is_preserved(publication):
    client, run, _ = publication
    run()
    client.documents["document"]["content"] = client.documents["document"]["content"].replace(
        "先检查连接池", "负责人手写的新判断")
    with pytest.raises(publisher.PublishError, match="未核验的修改"):
        run('<p>新的模型结论。</p>')
    assert client.update_count == 0
    assert "负责人手写的新判断" in client.documents["document"]["content"]


def test_user_resource_inside_owned_range_is_preserved(publication):
    client, run, _ = publication
    run()
    doc = client.documents["document"]
    marker = publisher._markers(DAY)[1]
    doc["content"] = doc["content"].replace(
        marker, f'<img token="user_image"/>{marker}')
    with pytest.raises(publisher.PublishError, match="未核验的修改"):
        run('<p>新的模型结论。</p>')
    assert client.update_count == 0 and "user_image" in doc["content"]


def test_lost_create_response_recovers_by_listing_without_second_create(publication):
    client, run, _ = publication
    client.lose_create_response = True
    assert run()["status"] == "created"
    assert run()["status"] == "unchanged"
    assert client.create_count == 1


def test_unknown_create_is_persisted_and_never_blindly_retried(publication):
    client, run, path = publication
    client.create_no_effect = True
    with pytest.raises(publisher.PublishError, match="已保存状态"):
        run()
    assert json.loads(next(path.glob("*.json")).read_text())["phase"] == "creating"
    with pytest.raises(publisher.PublishError, match="不会再次创建"):
        run()
    assert client.create_count == 1


def test_late_created_document_is_recovered_on_next_run(publication):
    client, run, _ = publication
    client.create_no_effect = True
    with pytest.raises(publisher.PublishError):
        run()
    client.add_document(publisher._document_xml(DAY, BODY)[0])
    assert run()["status"] == "unchanged"
    assert client.create_count == 1


def test_lost_update_response_is_read_back_not_repeated(publication):
    client, run, _ = publication
    run()
    client.lose_update_response = True
    assert run('<p>核实后的新结论。</p>')["status"] == "updated"
    assert client.update_count == 1


def test_foreign_same_title_is_not_overwritten_or_duplicated(publication):
    client, run, _ = publication
    client.add_document('<title>用户自己的日报</title><p>人工记录</p>')
    with pytest.raises(publisher.PublishError, match="归属标记"):
        run()
    assert client.create_count == 0 and client.update_count == 0


def test_duplicate_candidates_are_not_modified(publication):
    client, run, _ = publication
    client.add_document(publisher._document_xml(DAY, BODY)[0])
    client.nodes.append({**client.nodes[0], "node_token": "other", "obj_token": "other_doc"})
    with pytest.raises(publisher.PublishError, match="多个候选"):
        run()
    assert client.create_count == 0 and client.update_count == 0


def test_moved_report_is_not_recreated(publication):
    client, run, _ = publication
    run()
    client.nodes.clear()
    with pytest.raises(publisher.PublishError, match="不在原目录"):
        run()
    assert client.create_count == 1


def test_failed_draft_check_does_not_write_remote_or_creation_intent(publication):
    client, run, path = publication
    client.parse_status = "failed"
    with pytest.raises(publisher.PublishError, match="草稿校验未通过"):
        run()
    assert client.create_count == 0 and not list(path.glob("*.json"))


def test_xml_body_is_not_interpreted_as_shell(publication):
    client, run, _ = publication
    text = '<p>`touch /tmp/should-not-exist` $(cat .env) A &amp; B &lt; C</p>'
    run(text)
    assert "$(cat .env)" in client.documents["document"]["content"]


@pytest.mark.parametrize("body", ["", "<title>wrong</title>", "<!DOCTYPE test><p>x</p>", "<p>x"])
def test_invalid_body_never_calls_cli(publication, body):
    client, run, _ = publication
    with pytest.raises(publisher.PublishError):
        run(body)
    assert not client.calls


def test_cli_uses_user_identity_and_ignores_update_notice(monkeypatch):
    process = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
        "ok": True, "identity": "user", "data": {"safe": True},
        "_notice": {"update": "ignored"},
    }), ""))
    monkeypatch.setattr(publisher.subprocess, "run", process)
    assert publisher.LarkCLI().run(["wiki", "+node-list"]) == {"safe": True}
    argv = process.call_args.args[0]
    assert argv[-4:] == ["--as", "user", "--format", "json"]
    assert "shell" not in process.call_args.kwargs


def test_cli_error_does_not_expose_secret_or_raw_message(monkeypatch):
    monkeypatch.setattr(publisher.subprocess, "run", Mock(return_value=
        subprocess.CompletedProcess([], 1, "", json.dumps({"ok": False, "error": {
            "type": "authorization", "message": "secret access_token=private",
        }}))))
    with pytest.raises(publisher.PublishError) as error:
        publisher.LarkCLI().run(["wiki", "+node-list"])
    assert "private" not in str(error.value) and "authorization" in str(error.value)
