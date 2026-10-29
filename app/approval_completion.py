"""Read-only evidence for the bound user's individual approval task outcome."""

COMPLETED_TASK_STATUSES = frozenset({"APPROVED", "REJECTED", "TRANSFERRED", "DONE"})


def completed_owner_task_status(detail, *, instance_code, task_id, owner_open_id):
    """Return a terminal task status only when instance, task and owner agree.

    The overall instance status and absence from pending are not evidence of
    this user's decision. Duplicate task IDs are ambiguous even when one of
    the duplicates appears to match the expected owner.
    """
    if (
        not all(isinstance(value, str) and value for value in (instance_code, task_id, owner_open_id))
        or not isinstance(detail, dict) or detail.get("instance_code") != instance_code
        or detail.get("_source")
        or not isinstance(detail.get("tasks"), list)
    ):
        return None
    matches = [task for task in detail["tasks"] if isinstance(task, dict) and task.get("id") == task_id]
    if len(matches) != 1 or matches[0].get("user_id") != owner_open_id:
        return None
    status = matches[0].get("status")
    return status if isinstance(status, str) and status in COMPLETED_TASK_STATUSES else None
