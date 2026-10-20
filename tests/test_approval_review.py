import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.approval_review import (
    ApprovalReviewer,
    evaluate_policy,
    form_fingerprint,
    policy_summary,
    review_view,
)
from app.errors import FeishuError
from app.models import Account, ApprovalReview, ApprovalTask, SyncState, utcnow


def sample_task(task_id="task_1"):
    return {
        "task_id": task_id,
        "instance_code": f"instance_{task_id}",
        "definition_code": "definition_test",
        "definition_name": "测试环境申请",
        "user_id": "ou_owner",
        "status": 1,
        "topic": 1,
        "instance_status": 1,
        "support_api_operate": True,
    }


def sample_detail(task_id="task_1"):
    return {
        "instance_code": f"instance_{task_id}",
        "definition_code": "definition_test",
        "definition_name": "测试环境申请",
        "status": "PENDING",
        "reverted": False,
        "form": json.dumps([
            {"id": "description", "name": "说明", "type": "text", "value": "不构成授权"},
            {"id": "environment", "name": "环境", "type": "input", "value": "隔离测试"},
        ]),
        "tasks": [
            {"id": task_id, "status": "PENDING", "user_id": "ou_owner", "node_id": "node_1"},
        ],
        "current_nodes": [{
            "node_id": "node_1", "type": "AND",
            "approvers": [{"task_id": task_id, "user_id": "ou_owner"}],
        }],
    }


def sample_policy():
    return {
        "version": "test-v1",
        "rules": [{
            "id": "isolated-test",
            "definition_code": "definition_test",
            "definition_name": "测试环境申请",
            "rationale": "测试专用环境已由负责人核验，无真实变更。",
            "fields": {
                "environment": {"name": "环境", "type": "input", "values": ["隔离测试"]},
            },
        }],
    }


@pytest.mark.parametrize("name,expected", [
    ("工程平台-应用申请", "仓库归属"),
    ("工程平台-应用变更", "依赖切换"),
    ("从未配置的审批", "没有覆盖"),
])
def test_unverified_business_cases_require_review(name, expected):
    detail = sample_detail()
    detail["definition_name"] = name
    decision, reasons, rule = evaluate_policy({"version": "empty", "rules": []}, detail)
    assert decision == "needs_review"
    assert any(expected in reason for reason in reasons)
    assert rule is None


def test_exact_policy_checks_all_fields_types_names_and_definition():
    policy, detail = sample_policy(), sample_detail()
    assert evaluate_policy(policy, detail)[0] == "eligible"
    for changes in (
        {"value": "生产"}, {"name": "其他含义"}, {"type": "textarea"}, {"value": True},
    ):
        changed = deepcopy(detail)
        form = json.loads(changed["form"])
        form[1].update(changes)
        changed["form"] = json.dumps(form)
        assert evaluate_policy(policy, changed)[0] == "needs_review"
    for field in ("definition_code", "definition_name"):
        changed = {**detail, field: "new"}
        assert evaluate_policy(policy, changed)[0] == "needs_review"
    form = json.loads(detail["form"])
    form.append({"id": "extra", "name": "新增字段", "type": "input", "value": "额外申请"})
    assert evaluate_policy(policy, {**detail, "form": form})[0] == "needs_review"
    assert evaluate_policy(policy, {**detail, "form": "{broken"})[0] == "needs_review"
    assert evaluate_policy(policy, {**detail, "form": []})[0] == "needs_review"


def test_policy_validation_rejects_unknown_keys_and_duplicate_json_keys(tmp_path):
    path = tmp_path / "policy.json"
    settings = SimpleNamespace(approval_policy_path=str(path))
    path.write_text(json.dumps(sample_policy()))
    assert policy_summary(settings) == {"version": "test-v1", "rule_count": 1, "error_code": None}
    policy = sample_policy()
    policy["rules"][0]["approve_everything"] = True
    path.write_text(json.dumps(policy))
    assert policy_summary(settings)["error_code"] == "invalid_approval_policy"
    path.write_text('{"version":"a","version":"b","rules":[]}')
    assert policy_summary(settings)["rule_count"] == 0
    assert policy_summary(settings)["error_code"] == "invalid_approval_policy"
    assert review_view(None) is None


def test_single_task_authorization_never_extends_to_another_task_or_instance():
    policy = sample_policy()
    policy["rules"][0]["task_ids"] = ["task_1"]
    policy["rules"][0]["instance_codes"] = ["instance_task_1"]
    assert evaluate_policy(policy, sample_detail(), task_id="task_1")[0] == "eligible"
    assert evaluate_policy(policy, sample_detail(), task_id="task_2")[0] == "needs_review"
    changed = {**sample_detail(), "instance_code": "another_instance"}
    assert evaluate_policy(policy, changed, task_id="task_1")[0] == "needs_review"


def test_form_digest_binds_explanatory_text_ext_and_option_metadata():
    policy, detail = sample_policy(), sample_detail()
    form = json.loads(detail["form"])
    form[1]["option"] = {"label": "隔离测试", "id": "option_1"}
    detail["form"] = json.dumps(form)
    # Generic rules cannot silently discard option metadata.
    assert evaluate_policy(policy, detail)[0] == "needs_review"
    policy["rules"][0]["form_sha256"] = form_fingerprint(detail["form"])
    assert evaluate_policy(policy, detail)[0] == "eligible"
    for index, field, value in (
        (0, "value", "说明已更改"),
        (1, "ext", {"route": "/new"}),
        (1, "option", {"label": "隔离测试", "id": "different"}),
    ):
        changed = deepcopy(form)
        changed[index][field] = value
        assert evaluate_policy(policy, {**detail, "form": changed})[0] == "needs_review"


class FakeFeishu:
    def __init__(self):
        self.pending = [sample_task()]
        self.details = {"instance_task_1": sample_detail()}
        self.posts = []
        self.reads = 0
        self.post_error = None
        self.before_post = None
        self.approve_on_post = True
        self.has_more = False

    def tasks(self, topic, page_token):
        assert topic == "1" and page_token is None
        self.reads += 1
        return {"tasks": deepcopy(self.pending), "has_more": self.has_more}

    def instance(self, instance_code):
        self.reads += 1
        return deepcopy(self.details[instance_code])

    def approve_task(self, instance_code, task_id, comment):
        if self.before_post:
            self.before_post()
        self.posts.append((instance_code, task_id, comment))
        if self.post_error:
            raise self.post_error
        if self.approve_on_post:
            self.details[instance_code]["tasks"][0]["status"] = "APPROVED"
        return {}


@pytest.fixture
def review_setup(database, tmp_path):
    sessions = sessionmaker(database, expire_on_commit=False)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(sample_policy()))
    settings = SimpleNamespace(approval_automation_enabled=True, approval_policy_path=str(path))
    feishu = FakeFeishu()
    with sessions.begin() as db:
        db.add(Account(
            id=1, open_id="ou_owner", name="测试", access_token="not-real",
            expires_at=utcnow() + timedelta(hours=1), scope="approval:task:write",
        ))
        db.add(SyncState(
            resource="approvals:1", generation="generation_1", has_more=False,
            parameters={}, synced_at=utcnow(), completed_at=utcnow(),
        ))
        db.add(ApprovalTask(
            task_id="task_1", topic="1", instance_code="instance_task_1", title="测试申请",
            status="1", initiator_name="测试申请人", raw=sample_task(),
            visible=True, generation="generation_1", synced_at=utcnow(),
        ))
    return ApprovalReviewer(settings, sessions, feishu), sessions, feishu


def make_due(sessions, task_id="task_1"):
    with sessions.begin() as db:
        db.get(ApprovalReview, task_id).reviewed_at = utcnow() - timedelta(seconds=61)


@pytest.mark.integration
def test_submission_commits_before_post_and_confirms_own_task_only_once(review_setup):
    reviewer, sessions, feishu = review_setup

    def check_marker():
        with sessions() as db:
            row = db.get(ApprovalReview, "task_1")
            assert row.execution_status == "submitting"
            assert row.submitted_at is not None
        # Another reviewer must fail the advisory lock while the first writes.
        assert reviewer.tick() is None

    feishu.before_post = check_marker
    result = reviewer.tick()
    assert result["decision"] == "eligible"
    assert result["execution_status"] == "approved"
    assert result["confirmed_at"]
    assert "owner_open_id" not in result
    assert feishu.details["instance_task_1"]["status"] == "PENDING"
    make_due(sessions)
    assert reviewer.tick() is None
    assert len(feishu.posts) == 1


@pytest.mark.integration
def test_row_lock_observes_other_committed_marker_even_if_advisory_guard_is_lost(review_setup):
    reviewer, sessions, feishu = review_setup
    with sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:read"
    assert reviewer.tick()["execution_status"] == "blocked_permission"
    with sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:write"
    make_due(sessions)
    with sessions() as db:
        engine = db.get_bind()
    attempted_lock = Event()

    def observe_lock(_conn, _cursor, statement, _params, _context, _many):
        if "approval_reviews" in statement and "FOR UPDATE" in statement:
            attempted_lock.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with sessions.begin() as first_writer:
            row = first_writer.scalar(
                select(ApprovalReview).where(
                    ApprovalReview.task_id == "task_1"
                ).with_for_update()
            )
            row.execution_status, row.submitted_at = "submitting", utcnow()
            first_writer.flush()
            event.listen(engine, "before_cursor_execute", observe_lock)
            try:
                # Skip the advisory guard to emulate its connection being lost.
                # This reader sees the previous committed row until FOR UPDATE.
                second = executor.submit(reviewer._tick)
                assert attempted_lock.wait(5)
                assert not second.done()
                assert not feishu.posts
            finally:
                event.remove(engine, "before_cursor_execute", observe_lock)
        # On release, the second writer must observe the committed marker and
        # stop before POST, rather than overwrite the first writer's decision.
        assert second.result(timeout=5)["execution_status"] == "submitting"
    assert not feishu.posts


@pytest.mark.integration
def test_missing_permission_preserves_business_decision_and_rechecks_when_granted(review_setup):
    reviewer, sessions, feishu = review_setup
    with sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:read"
    result = reviewer.tick()
    assert result["decision"] == "eligible"
    assert result["execution_status"] == "blocked_permission"
    assert result["submitted_at"] is None
    assert not feishu.posts
    with sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:write"
    make_due(sessions)
    assert reviewer.tick()["execution_status"] == "approved"
    assert len(feishu.posts) == 1


@pytest.mark.integration
def test_business_review_takes_precedence_over_missing_scope(review_setup):
    reviewer, sessions, feishu = review_setup
    with sessions.begin() as db:
        db.get(Account, 1).scope = "approval:task:read"
    feishu.details["instance_task_1"]["definition_name"] = "工程平台-应用变更"
    result = reviewer.tick()
    assert result["decision"] == "needs_review"
    assert result["execution_status"] == "awaiting_review"
    assert any("依赖切换" in reason for reason in result["reasons"])
    assert not feishu.posts


@pytest.mark.integration
@pytest.mark.parametrize("change", [
    "wrong_owner", "unsupported", "reverted", "task_done", "inactive_node",
    "cross_instance", "new_form_field", "not_in_fresh_pending",
])
def test_fresh_data_and_current_ownership_gate_execution(review_setup, change):
    reviewer, _, feishu = review_setup
    detail = feishu.details["instance_task_1"]
    if change == "wrong_owner":
        feishu.pending[0]["user_id"] = "ou_somebody_else"
    elif change == "unsupported":
        feishu.pending[0]["support_api_operate"] = False
    elif change == "reverted":
        detail["reverted"] = True
    elif change == "task_done":
        detail["tasks"][0]["status"] = "APPROVED"
    elif change == "inactive_node":
        detail["current_nodes"][0]["approvers"][0]["user_id"] = "ou_somebody_else"
    elif change == "cross_instance":
        feishu.pending[0]["instance_code"] = "other_instance"
    elif change == "new_form_field":
        detail["form"] = json.loads(detail["form"]) + [
            {"id": "new", "name": "新增", "type": "input", "value": "更大变更"},
        ]
    elif change == "not_in_fresh_pending":
        feishu.pending = []
    result = reviewer.tick()
    assert result["decision"] in {"needs_review", "not_pending"}
    assert result["submitted_at"] is None
    assert not feishu.posts


@pytest.mark.integration
def test_timeout_is_never_replayed_and_later_task_read_can_confirm(review_setup):
    reviewer, sessions, feishu = review_setup
    feishu.post_error = FeishuError("network")
    result = reviewer.tick()
    assert result["execution_status"] == "unknown"
    assert len(feishu.posts) == 1
    assert reviewer.tick() is None  # 60 second cooldown.
    make_due(sessions)
    assert reviewer.tick()["execution_status"] == "unknown"
    assert len(feishu.posts) == 1
    # Disappearance or instance completion alone cannot confirm the user's task.
    feishu.pending = []
    feishu.details["instance_task_1"]["status"] = "APPROVED"
    make_due(sessions)
    assert reviewer.tick()["execution_status"] == "unknown"
    feishu.details["instance_task_1"]["tasks"][0]["status"] = "APPROVED"
    make_due(sessions)
    assert reviewer.tick()["execution_status"] == "approved"
    assert len(feishu.posts) == 1


@pytest.mark.integration
def test_crashed_submitting_marker_is_reconciled_without_another_post(review_setup):
    reviewer, sessions, feishu = review_setup
    feishu.post_error = FeishuError("network")
    reviewer.tick()
    with sessions.begin() as db:
        row = db.get(ApprovalReview, "task_1")
        row.execution_status = "submitting"
        row.reviewed_at = utcnow() - timedelta(seconds=61)
        db.get(ApprovalTask, ("task_1", "1")).visible = False
        db.get(SyncState, "approvals:1").completed_at = utcnow() - timedelta(hours=1)
    assert reviewer.tick()["execution_status"] == "unknown"
    assert len(feishu.posts) == 1


@pytest.mark.integration
def test_fair_rotation_cooldown_and_incomplete_snapshot(review_setup):
    reviewer, sessions, feishu = review_setup
    policy = {"version": "empty", "rules": []}
    Path(reviewer.settings.approval_policy_path).write_text(json.dumps(policy))
    feishu.pending.append(sample_task("task_2"))
    feishu.details["instance_task_2"] = sample_detail("task_2")
    with sessions.begin() as db:
        db.add(ApprovalTask(
            task_id="task_2", topic="1", instance_code="instance_task_2", title="测试申请2",
            status="1", initiator_name="测试申请人", raw=sample_task("task_2"),
            visible=True, generation="generation_1", synced_at=utcnow(),
        ))
        db.get(SyncState, "approvals:1").has_more = True
    assert reviewer.tick() is None
    with sessions.begin() as db:
        db.get(SyncState, "approvals:1").has_more = False
    assert reviewer.tick()["task_id"] == "task_1"
    assert reviewer.tick()["task_id"] == "task_2"
    assert reviewer.tick() is None
    assert feishu.reads == 4
    with sessions() as db:
        assert len(db.scalars(select(ApprovalReview)).all()) == 2
    assert not feishu.posts
