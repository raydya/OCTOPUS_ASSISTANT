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
from app.models import (
    Account,
    ApprovalInstance,
    ApprovalTask,
    StatusNoticeSource,
    StatusNotification,
)
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
    notices.item = {**raw, "status": 2, "topic": 2, "instance_status": 2}
    notices.calls = []

    def native(code):
        notices.calls.append(("native", code))
        raise FeishuError("1390001")

    def lookup(code, *, expected_owner, start_timestamp, end_timestamp, page_token=None):
        assert 0 < int(end_timestamp) - int(start_timestamp) <= 14 * 86400
        assert code == raw["definition_code"]
        notices.calls.append(("external", code, expected_owner, page_token))
        return {"tasks": [deepcopy(notices.item)], "has_more": False}

    notices.feishu.instance = native
    notices.feishu.personal_handled_approval_tasks = lookup
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
    assert external.calls == [("external", external.raw_task["definition_code"], "ou_owner", None)]
    assert len(external.feishu.created_cards) == 1
    assert len(external.feishu.updated_cards) == before_updates + 1
    assert external.feishu.updated_cards[-1][0] == original
    assert external.feishu.updated_cards[-1][1]["elements"][0]["text"]["content"] == "✅ 已处理 · 流程已通过"
    with external.sessions() as db:
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        assert detail["_source"] == "personal_approval_tasks"
        assert not {"form", "current_nodes", "definition_code", "reverted", "tasks"} & detail.keys()
        assert _eligibility_errors(external.raw_task, detail, "task_1", "instance_task_1", "ou_owner")
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "processed"


@pytest.mark.parametrize("status,label", [
    (0, "✅ 已处理"), (1, "✅ 已处理"),
    (3, "ℹ️ 已处理 · 流程已拒绝"), (4, "ℹ️ 已处理 · 流程已撤销"),
    (5, "ℹ️ 已处理 · 流程已终止"),
])
def test_handled_status_separates_user_processing_from_workflow_result(external, status, label):
    external.item["instance_status"] = status
    refresh(external)
    external.service.tick()
    assert external.feishu.updated_cards[-1][1]["elements"][0]["text"]["content"] == label


@pytest.mark.parametrize("key,value", [
    ("user_id", "ou_other"), ("task_id", "task_other"),
    ("task_external_id", "external_other"),
    ("instance_code", "instance_other"), ("instance_external_id", "people_other"),
    ("definition_code", "definition_other"), ("topic", 1),
    ("status", 1), ("status", "future_unknown_status"), ("instance_status", True),
])
def test_mismatched_or_nonterminal_evidence_never_completes(external, key, value):
    external.item[key] = value
    try:
        refresh(external)
    except AppError:
        pass
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] not in {"approved", "processed"}


@pytest.mark.parametrize("mode", ["duplicate", "incomplete", "repeated_cursor", "malformed", "permission"])
def test_ambiguous_partial_or_failed_reads_never_store_completion(external, mode):
    def lookup(code, **kwargs):
        if mode == "permission":
            raise FeishuError("99991672")
        if mode == "duplicate":
            return {"tasks": [external.item, external.item], "has_more": False}
        if mode == "malformed":
            return {"tasks": {}, "has_more": False}
        cursor = kwargs.get("page_token")
        return {"tasks": [external.item], "has_more": True,
                "page_token": "next" if mode == "repeated_cursor" or not cursor else "third"}
    external.feishu.personal_handled_approval_tasks = lookup
    with pytest.raises(AppError):
        refresh(external)
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1") is None


def test_complete_pagination_is_checked_before_accepting_result(external):
    def lookup(code, **kwargs):
        if kwargs.get("page_token"):
            return {"tasks": [external.item], "has_more": False}
        return {"tasks": [], "has_more": True, "page_token": "next"}
    external.feishu.personal_handled_approval_tasks = lookup
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "processed"


@pytest.mark.parametrize("change", ["owner", "task_binding", "newer_pending"])
def test_changed_account_or_task_during_lookup_discards_result(external, change):
    original = external.feishu.personal_handled_approval_tasks
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
    external.feishu.personal_handled_approval_tasks = lookup
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
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] not in {"approved", "processed"}


def test_empty_search_does_not_infer_completion(external):
    external.feishu.personal_handled_approval_tasks = lambda *a, **k: {"tasks": [], "has_more": False}
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1").raw["handled_tasks"] == []
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "responded"


def test_external_pending_evidence_does_not_grant_native_execution_eligibility(external):
    external.item["status"] = 1
    refresh(external)
    with external.sessions() as db:
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        assert _eligibility_errors(external.raw_task, detail, "task_1", "instance_task_1", "ou_owner")


def test_refresh_after_completion_preserves_result_without_another_lookup(external):
    refresh(external)
    external.service.tick()
    before = len(external.calls)
    refresh(external)
    external.service.tick()
    assert len(external.calls) == before
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "processed"


def test_second_task_in_same_instance_does_not_erase_first_result(external):
    refresh(external)
    external.service.tick()
    external.clock.now += timedelta(seconds=1)
    second = add_pending(external, "task_2")
    second.update(instance_code="instance_task_1", instance_external_id="people_ac_example",
                  task_external_id="external_task_2")
    with external.sessions.begin() as db:
        task = db.get(ApprovalTask, ("task_2", "1"))
        task.instance_code, task.raw = "instance_task_1", second
    external.service.tick()
    with external.sessions.begin() as db:
        db.get(ApprovalTask, ("task_2", "1")).visible = False
    external.service.tick()
    external.item = {**second, "status": 2, "topic": 2, "instance_status": 2}
    external.clock.now += timedelta(seconds=1)
    refresh(external)
    external.service.tick()
    with external.sessions() as db:
        sources = list(db.scalars(select(StatusNoticeSource).where(StatusNoticeSource.source_type == "approval")))
        assert len(sources) == 2
        assert all(source.payload["phase"] == "processed" for source in sources)
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        observations = {item["task_id"]: item["observed_at"] for item in detail["handled_tasks"]}
        assert observations["task_1"] < observations["task_2"]


@pytest.mark.parametrize("change", ["pending", "untracked", "old", "foreign_owner", "already_processed"])
def test_only_recent_notified_inactive_tasks_trigger_lookup(external, change):
    with external.sessions.begin() as db:
        source = db.scalar(select(StatusNoticeSource))
        if change == "pending":
            db.get(ApprovalTask, ("task_1", "1")).visible = True
        elif change == "untracked":
            source.card_create_state, source.card_message_id = "none", None
        elif change == "old":
            for notice in db.scalars(select(StatusNotification)):
                notice.sent_at = external.clock.now - timedelta(days=15)
        elif change == "foreign_owner":
            source.owner_open_id = "ou_other"
        else:
            source.payload = {**source.payload, "phase": "processed"}
    assert refresh(external)["items"] == 0
    assert external.calls == []
    with external.sessions() as db:
        assert db.get(ApprovalInstance, "instance_task_1") is None


def test_unrelated_personal_history_and_content_are_not_persisted(external):
    unrelated = {**external.item, "task_id": "untracked", "summaries": ["PRIVATE_OTHER_CONTENT"]}
    wanted = {**external.item, "summaries": ["PRIVATE_FORM_CONTENT"]}
    external.feishu.personal_handled_approval_tasks = lambda *a, **k: {
        "tasks": [unrelated, wanted], "has_more": False,
    }
    refresh(external)
    with external.sessions() as db:
        detail = db.get(ApprovalInstance, "instance_task_1").raw
        assert len(detail["handled_tasks"]) == 1
        assert "PRIVATE_" not in str(detail)
        assert db.get(ApprovalTask, ("untracked", "2")) is None
        assert not list(db.scalars(select(ApprovalTask).where(ApprovalTask.topic == "2")))


def test_old_tenant_projection_is_not_used_as_personal_completion(external):
    with external.sessions.begin() as db:
        db.add(ApprovalInstance(
            instance_code="instance_task_1", synced_at=external.clock.now,
            raw={"instance_code": "instance_task_1", "_source": "external_task_search",
                 "tasks": [{"id": "task_1", "user_id": "ou_owner", "status": "APPROVED"}]},
        ))
    external.service.tick()
    with external.sessions() as db:
        assert db.scalar(select(StatusNoticeSource)).payload["phase"] == "responded"
