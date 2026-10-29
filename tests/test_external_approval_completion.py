"""External attendance approvals finish their original reminder, without native writes."""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_status_notifications import activate, add_pending
from test_status_notifications import notices as notices

from app import sync as sync_module
from app.approval_review import _eligibility_errors
from app.errors import AppError, FeishuError
from app.models import Account, ApprovalInstance, ApprovalTask, StatusNoticeSource
from app.sync import SyncService

pytestmark = pytest.mark.integration


@pytest.fixture
def external(notices, monkeypatch):
    activate(notices)
    raw = add_pending(notices)
    raw.update(instance_external_id="people_ac_example", task_external_id="external_task")
    with notices.sessions.begin() as db:
        db.get(ApprovalTask, ("task_1", "1")).raw = raw
    notices.service.tick()
    with notices.sessions.begin() as db:
        db.get(ApprovalTask, ("task_1", "1")).visible = False
    notices.service.tick()
    notices.clock.now += timedelta(seconds=1)
    monkeypatch.setattr(sync_module, "utcnow", lambda: notices.clock.now)
    notices.settings.sync_page_limit = 2
    notices.item = {
        "approval": {"code": raw["definition_code"], "is_external": True},
        "instance": {
            "code": raw["instance_code"], "external_id": raw["instance_external_id"],
            "status": "pending",
        },
        "task": {
            "task_id": "task_1", "task_external_id": "external_task",
            "user_id": "ou_owner", "status": "approved",
        },
    }
    notices.calls = []

    def native(code):
        notices.calls.append(("native", code))
        raise FeishuError("1390001")

    def lookup(code, *, expected_owner, page_token=None):
        notices.calls.append(("external", code, expected_owner, page_token))
        return {"task_list": [deepcopy(notices.item)], "has_more": False}

    notices.feishu.instance = native
    notices.feishu.external_approval_tasks = lookup
    notices.sync = SyncService(notices.settings, notices.sessions, notices.feishu)
    notices.raw_task = raw
    return notices


def refresh(system):
    return system.sync.run("instance:instance_task_1")


def test_external_remedy_approval_finishes_same_card_and_stays_read_only(external):
    with external.sessions() as db:
        original = db.scalar(select(StatusNoticeSource)).card_message_id
    before_updates = len(external.feishu.updated_cards)
    refresh(external)
    external.service.tick()
    external.service.tick()
    assert external.calls == [("external", "instance_task_1", "ou_owner", None)]
    assert len(external.feishu.created_cards) == 1
    assert len(external.feishu.updated_cards) == before_updates + 1
    assert external.feishu.updated_cards[-1][0] == original
    assert external.feishu.updated_cards[-1][1]["elements"][0]["text"]["content"] == "✅ 已通过"
    with external.sessions() as db:
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        assert detail["_source"] == "external_task_search"
        assert not {"form", "current_nodes", "definition_code", "reverted"} & detail.keys()
        assert _eligibility_errors(external.raw_task, detail, "task_1", "instance_task_1", "ou_owner")
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "approved"


@pytest.mark.parametrize("status,label", [
    ("rejected", "❌ 已拒绝"), ("transferred", "↪️ 已转交"),
    ("done", "ℹ️ 本人节点已结束"),
])
def test_external_completion_uses_owner_task_not_overall_workflow(external, status, label):
    external.item["task"]["status"] = status
    external.item["instance"]["status"] = "approved"
    refresh(external)
    external.service.tick()
    assert external.feishu.updated_cards[-1][1]["elements"][0]["text"]["content"] == label


@pytest.mark.parametrize("section,key,value", [
    ("task", "user_id", "ou_other"), ("task", "task_id", "task_other"),
    ("task", "task_external_id", "external_other"),
    ("instance", "code", "instance_other"), ("instance", "external_id", "people_other"),
    ("approval", "code", "definition_other"), ("approval", "is_external", False),
    ("task", "status", "pending"), ("task", "status", "future_unknown_status"),
])
def test_mismatched_or_nonterminal_evidence_never_approves(external, section, key, value):
    external.item[section][key] = value
    external.item["instance"]["status"] = "approved"
    try:
        refresh(external)
    except AppError:
        pass
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] != "approved"


@pytest.mark.parametrize("mode", ["duplicate", "incomplete", "repeated_cursor", "malformed", "permission"])
def test_ambiguous_partial_or_failed_reads_never_store_completion(external, mode):
    def lookup(code, **kwargs):
        if mode == "permission":
            raise FeishuError("99991672")
        if mode == "duplicate":
            return {"task_list": [external.item, external.item], "has_more": False}
        if mode == "malformed":
            return {"task_list": {}, "has_more": False}
        cursor = kwargs.get("page_token")
        return {"task_list": [external.item], "has_more": True,
                "page_token": "next" if mode == "repeated_cursor" or not cursor else "third"}
    external.feishu.external_approval_tasks = lookup
    with pytest.raises(AppError):
        refresh(external)
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1") is None


def test_complete_pagination_is_checked_before_accepting_result(external):
    def lookup(code, **kwargs):
        if kwargs.get("page_token"):
            return {"task_list": [external.item], "has_more": False}
        return {"task_list": [], "has_more": True, "page_token": "next"}
    external.feishu.external_approval_tasks = lookup
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "approved"


@pytest.mark.parametrize("change", ["owner", "task_binding", "newer_pending"])
def test_changed_account_or_task_during_lookup_discards_result(external, change):
    original = external.feishu.external_approval_tasks
    def lookup(code, **kwargs):
        result = original(code, **kwargs)
        with external.sessions.begin() as db:
            task = db.get(ApprovalTask, ("task_1", "1"))
            if change == "owner":
                db.get(Account, 1).open_id = "ou_other"
            elif change == "task_binding":
                task.raw = {**task.raw, "task_external_id": "replacement"}
            else:
                task.visible = True
                task.synced_at = external.clock.now + timedelta(seconds=1)
        return result
    external.feishu.external_approval_tasks = lookup
    with pytest.raises(AppError):
        refresh(external)
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1") is None


def test_native_instance_read_keeps_existing_api(external):
    with external.sessions.begin() as db:
        row = db.get(ApprovalTask, ("task_1", "1"))
        row.raw = {**row.raw, "instance_external_id": "", "task_external_id": ""}
    with pytest.raises(FeishuError, match="飞书请求失败"):
        refresh(external)
    assert external.calls == [("native", "instance_task_1")]


def test_external_search_lag_cannot_complete_a_reopened_pending_task(external):
    refresh(external)
    external.service.tick()
    with external.sessions.begin() as db:
        task = db.get(ApprovalTask, ("task_1", "1"))
        task.visible = True
        task.synced_at = external.clock.now
    external.clock.now += timedelta(seconds=1)
    # Even a newly fetched search index can contain the old approved result.
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] != "approved"


def test_empty_search_does_not_infer_completion(external):
    external.feishu.external_approval_tasks = lambda *a, **k: {"task_list": [], "has_more": False}
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1").raw["tasks"] == []
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "responded"


def test_optional_external_task_id_can_be_absent_when_internal_identity_agrees(external):
    del external.item["task"]["task_external_id"]
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "approved"


def test_external_pending_evidence_does_not_grant_native_execution_eligibility(external):
    external.item["task"]["status"] = "pending"
    refresh(external)
    with external.sessions() as db:
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        assert _eligibility_errors(external.raw_task, detail, "task_1", "instance_task_1", "ou_owner")
