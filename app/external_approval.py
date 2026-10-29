"""Narrow, read-only completion evidence for an owner's known external tasks."""

from app.approval_completion import COMPLETED_TASK_STATUSES
from app.errors import FeishuError

SOURCE = "external_task_search"
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
        })
    return sorted(bindings, key=lambda item: item["task_id"])


def completion_detail(items, bindings, instance_code):
    """Bind each remote result; never infer a decision from workflow status.

    This is deliberately not a native approval detail. It has neither form,
    definition, active nodes nor API approval eligibility.
    """
    tasks = []
    for binding in bindings:
        matches = [item for item in items if isinstance(item.get("task"), dict)
                   and item["task"].get("task_id") == binding["task_id"]]
        if not matches:
            # The search index may lag or filter revoked instances.
            continue
        if len(matches) != 1:
            raise FeishuError("invalid_external_approval")
        item = matches[0]
        approval, instance, task = item.get("approval"), item.get("instance"), item["task"]
        if (
            not isinstance(approval, dict) or not isinstance(instance, dict)
            or approval.get("is_external") is not True
            or approval.get("code") != binding["definition_code"]
            or instance.get("code") != instance_code
            or instance.get("external_id") != binding["instance_external_id"]
            or task.get("user_id") != binding["user_id"]
            or (task.get("task_external_id") is not None
                and task["task_external_id"] != binding["task_external_id"])
        ):
            raise FeishuError("invalid_external_approval")
        status = task.get("status")
        if not isinstance(status, str) or status.upper() not in COMPLETED_TASK_STATUSES | {"PENDING"}:
            continue
        tasks.append({
            "id": binding["task_id"], "user_id": binding["user_id"], "status": status.upper(),
            "external_id": binding["task_external_id"],
        })
    return {"instance_code": instance_code, "_source": SOURCE, "tasks": tasks}
