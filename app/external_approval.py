"""Reconcile known external reminders using the owner's personal handled list."""

from datetime import datetime

from app.errors import FeishuError

SOURCE = "personal_approval_tasks"
BINDING_FIELDS = (
    "task_id", "instance_code", "user_id", "definition_code",
    "instance_external_id", "task_external_id",
)


def external_bindings(tasks, instance_code, owner):
    """Snapshot identities from the authenticated personal pending list."""
    if not any(
        isinstance(task.raw, dict)
        and (task.raw.get("instance_external_id") or task.raw.get("task_external_id"))
        for task in tasks
    ):
        return []
    bindings = []
    for task in tasks:
        raw = task.raw
        if (
            not isinstance(raw, dict)
            or not all(isinstance(raw.get(key), str) and raw[key] for key in BINDING_FIELDS)
            or raw["task_id"] != task.task_id or raw["instance_code"] != instance_code
            or raw["user_id"] != owner
        ):
            raise FeishuError("invalid_external_approval")
        bindings.append({
            **{key: raw[key] for key in BINDING_FIELDS},
            "synced_at": task.synced_at.isoformat(),
            "visible": task.visible, "status": task.status,
        })
    return sorted(bindings, key=lambda item: item["task_id"])


def completion_detail(items, bindings, instance_code, *, observed_at):
    """Store only matched personal facts, without fabricating an APPROVED task."""
    tasks = []
    for binding in bindings:
        matches = [item for item in items if item.get("task_id") == binding["task_id"]]
        if not matches:
            # A bounded personal read may not contain this task. Stay unknown.
            continue
        if len(matches) != 1:
            raise FeishuError("invalid_external_approval")
        item = matches[0]
        if any(item.get(key) != binding[key] for key in BINDING_FIELDS):
            raise FeishuError("invalid_external_approval")
        if (
            str(item.get("status")) != "2" or str(item.get("topic")) != "2"
            or str(item.get("instance_status")) not in {"0", "1", "2", "3", "4", "5"}
        ):
            continue
        tasks.append({
            **{key: binding[key] for key in BINDING_FIELDS},
            "status": "2", "topic": "2", "instance_status": str(item["instance_status"]),
            "observed_at": observed_at.isoformat(),
        })
    return {"instance_code": instance_code, "_source": SOURCE, "handled_tasks": tasks}


def personal_handled_result(detail, task, *, not_before):
    """Return workflow status only with an exact personal handled-task match.

    A handled task is evidence of processing, not the user's particular action.
    The workflow outcome remains separate from the native task decision.
    """
    if (
        not isinstance(detail, dict) or detail.get("_source") != SOURCE
        or not isinstance(task, dict)
        or not all(isinstance(task.get(key), str) and task[key] for key in BINDING_FIELDS)
        or detail.get("instance_code") != task["instance_code"]
        or not isinstance(detail.get("handled_tasks"), list)
    ):
        return None
    matches = [item for item in detail["handled_tasks"] if isinstance(item, dict)
               and item.get("task_id") == task["task_id"]]
    if len(matches) != 1 or any(matches[0].get(key) != task[key] for key in BINDING_FIELDS):
        return None
    item = matches[0]
    if item.get("status") != "2" or item.get("topic") != "2":
        return None
    try:
        observed = datetime.fromisoformat(item["observed_at"])
        if (
            observed.tzinfo is None or not isinstance(not_before, datetime)
            or not_before.tzinfo is None or observed < not_before
        ):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    status = item.get("instance_status")
    return status if isinstance(status, str) and status in {"0", "1", "2", "3", "4", "5"} else None
