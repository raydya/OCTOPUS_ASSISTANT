import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.content import message_text, parse_json
from app.errors import AppError, FeishuError
from app.external_approval import completion_detail, external_bindings
from app.models import (
    Account,
    ApprovalInstance,
    ApprovalTask,
    Chat,
    Message,
    SyncRun,
    SyncState,
    utcnow,
)


def message_activity_time(raw: dict) -> int:
    """Use source timestamps, never the time we happened to fetch a message."""
    values = [0]
    for key in ("create_time", "update_time"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
            value = int(value)
        if type(value) is int and 0 < value <= 253402300799999:
            values.append(value)
    return max(values)


def upsert(db, model, values):
    keys = [column.name for column in model.__table__.primary_key]
    stmt = insert(model).values(**values)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=keys,
            set_={key: value for key, value in values.items() if key not in keys},
        )
    )


@dataclass(frozen=True)
class ReadPlan:
    resource: str
    kind: str
    identifier: str
    chat_id: str
    owner: tuple
    previous: dict | None
    generation: str
    parameters: dict
    page_token: str | None
    started: datetime
    instance_version: datetime | None = None


def _state_version(state):
    if state is None:
        return None
    return {
        key: deepcopy(getattr(state, key)) for key in (
            "generation", "parameters", "page_token", "has_more", "synced_at", "completed_at",
        )
    }


def _owner(account):
    return (account.open_id, account.tenant_key) if account else (None, None)


class SyncService:
    def __init__(self, settings, sessions, feishu):
        self.settings = settings
        self.sessions = sessions
        self.feishu = feishu

    def run(self, resource: str, *, days=7, restart=False) -> dict:
        started = utcnow()
        try:
            if resource == "dms_cards":
                from app.dms_sync import DmsCardSync

                return DmsCardSync(self.settings, self.sessions, self.feishu, self).run()
            plan = self._prepare(resource, days, restart, started)
            rows, has_more, cursor = self._read(plan)
            return self._commit(plan, rows, has_more, cursor)
        except AppError as exc:
            if exc.code != "sync_busy":
                with self.sessions.begin() as db:
                    db.add(
                        SyncRun(
                            id=str(uuid4()), resource=resource, status="failed", items=0,
                            error_code=exc.code, started_at=started, finished_at=utcnow(),
                        )
                    )
            raise

    @staticmethod
    def _write_lock(db):
        if not db.scalar(text("SELECT pg_try_advisory_xact_lock(81024002)")):
            raise AppError("sync_busy", "已有同步正在进行，请稍后重试。", 409)

    def _prepare(self, resource, days, restart, started):
        """Capture a cursor without reserving a DB connection during HTTP reads."""
        kind, _, identifier = resource.partition(":")
        if kind not in {"chats", "messages", "thread", "approvals", "instance"}:
            raise AppError("invalid_resource", "不支持的同步类型。")
        with self.sessions.begin() as db:
            # A short preflight preserves fail-fast behavior for active writers.
            # This transaction and its lock both end before any Feishu request.
            self._write_lock(db)
            owner = _owner(db.get(Account, 1))
            chat_id = identifier
            if kind == "messages" and not db.get(Chat, identifier):
                raise AppError("chat_not_found", "请先同步会话列表。", 404)
            if kind == "thread":
                parent = db.scalar(select(Message).where(Message.thread_id == identifier).limit(1))
                if not parent:
                    raise AppError("thread_not_found", "请先同步包含该话题的会话。", 404)
                chat_id = parent.chat_id
            if kind == "instance":
                if not db.scalar(select(ApprovalTask.task_id).where(
                    ApprovalTask.instance_code == identifier,
                ).limit(1)):
                    raise AppError("instance_not_found", "请先同步对应审批任务。", 404)
                instance = db.get(ApprovalInstance, identifier)
                bindings = self._external_bindings(db, identifier, owner[0])
                return ReadPlan(
                    resource, kind, identifier, chat_id, owner, None, "",
                    {"external_tasks": bindings} if bindings else {}, None,
                    started, instance.synced_at if instance else None,
                )
            state = db.get(SyncState, resource)
            previous = _state_version(state)
            if restart or state is None or not state.has_more:
                generation, cursor, parameters = str(uuid4()), None, {}
                if kind == "messages":
                    now = int(time.time())
                    parameters = {
                        "container_id_type": "chat",
                        "start_time": str(now - days * 86400), "end_time": str(now),
                    }
                elif kind == "thread":
                    parameters = {"container_id_type": "thread"}
                elif kind == "chats":
                    parameters = {"snapshot_started_at": started.isoformat()}
            else:
                generation, cursor, parameters = state.generation, state.page_token, deepcopy(state.parameters)
                if kind == "chats":
                    # Old deployments have no start marker. The prior successful
                    # page predates new positive membership observations.
                    parameters.setdefault("snapshot_started_at", (state.synced_at or started).isoformat())
            return ReadPlan(
                resource, kind, identifier, chat_id, owner, previous,
                generation, parameters, cursor, started,
            )

    def _read(self, plan):
        """Fetch a bounded batch with no application DB transaction or write lock."""
        if plan.kind == "instance":
            if plan.parameters.get("external_tasks"):
                return [self._read_external_completion(plan)], False, None
            return [self.feishu.instance(plan.identifier)], False, None
        cursor, has_more, rows = plan.page_token, True, []
        seen_tokens = {cursor}
        for _ in range(self.settings.sync_page_limit):
            if plan.kind == "chats":
                data = self.feishu.chats(cursor)
            elif plan.kind == "approvals":
                data = self.feishu.tasks(plan.identifier, cursor)
            else:
                data = self.feishu.messages(plan.identifier, plan.parameters, cursor)
            page = data.get("tasks" if plan.kind == "approvals" else "items")
            if not isinstance(page, list):
                raise FeishuError("invalid_response")
            rows.extend(page)
            has_more = bool(data.get("has_more", False))
            cursor = data.get("page_token") if has_more else None
            if has_more and (not isinstance(cursor, str) or not cursor or cursor in seen_tokens):
                raise AppError(
                    "invalid_pagination", "飞书返回了重复或缺失的分页标记，本批数据未提交。请重试或重新同步。", 502,
                )
            seen_tokens.add(cursor)
            if not has_more:
                break
        return rows, has_more, cursor

    @staticmethod
    def _external_bindings(db, identifier, owner):
        tasks = list(db.scalars(select(ApprovalTask).where(
            ApprovalTask.instance_code == identifier, ApprovalTask.topic == "1",
            ApprovalTask.raw["user_id"].as_string() == owner,
        ).order_by(ApprovalTask.task_id)))
        return external_bindings(tasks, identifier, owner)

    def _read_external_completion(self, plan):
        cursor, seen, items = None, {None}, []
        for _ in range(self.settings.sync_page_limit):
            data = self.feishu.external_approval_tasks(
                plan.identifier, expected_owner=plan.owner[0], page_token=cursor,
            )
            if (
                not isinstance(data, dict) or not isinstance(data.get("task_list"), list)
                or any(not isinstance(item, dict) for item in data["task_list"])
                or type(data.get("has_more")) is not bool
            ):
                raise FeishuError("invalid_response")
            items.extend(data["task_list"])
            if not data["has_more"]:
                return completion_detail(items, plan.parameters["external_tasks"], plan.identifier)
            cursor = data.get("page_token")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise FeishuError("invalid_pagination")
            seen.add(cursor)
        # A partial page cannot rule out conflicting duplicate task identities.
        raise FeishuError("external_approval_incomplete")

    def _commit(self, plan, rows, has_more, cursor):
        with self.sessions.begin() as db:
            self._write_lock(db)
            if _owner(db.get(Account, 1)) != plan.owner:
                raise AppError("sync_owner_changed", "同步绑定的账号发生变化，请重新同步。", 409)
            if plan.kind == "instance":
                cached = db.get(ApprovalInstance, plan.identifier)
                if (cached.synced_at if cached else None) != plan.instance_version:
                    raise AppError("sync_cursor_changed", "审批详情已更新，请稍后重试。", 409)
                if plan.parameters.get("external_tasks") and (
                    self._external_bindings(db, plan.identifier, plan.owner[0])
                    != plan.parameters["external_tasks"]
                ):
                    raise AppError("sync_cursor_changed", "审批任务已更新，请稍后重试。", 409)
                upsert(db, ApprovalInstance, {
                    "instance_code": plan.identifier, "raw": rows[0], "synced_at": utcnow(),
                })
            else:
                state = db.get(SyncState, plan.resource)
                if _state_version(state) != plan.previous:
                    raise AppError("sync_cursor_changed", "同步进度已更新，请稍后重试。", 409)
                if state is None:
                    state = SyncState(resource=plan.resource)
                    db.add(state)
                state.generation, state.parameters = plan.generation, deepcopy(plan.parameters)
                state.page_token, state.has_more = cursor, has_more
                for raw in rows:
                    self._store(
                        db, plan.kind, raw, plan.identifier, plan.chat_id, plan.generation,
                        read_started=plan.started,
                    )
                if plan.kind in {"messages", "thread"}:
                    latest = max((message_activity_time(raw) for raw in rows), default=0)
                    if latest:
                        db.execute(update(Chat).where(
                            Chat.chat_id == plan.chat_id,
                            or_(Chat.last_activity_time.is_(None), Chat.last_activity_time < latest),
                        ).values(last_activity_time=latest))
                if not has_more:
                    state.completed_at = utcnow()
                    if plan.kind == "approvals":
                        # Disappearing from pending does not mean approved.
                        db.execute(update(ApprovalTask).where(
                            ApprovalTask.topic == plan.identifier,
                            ApprovalTask.generation != plan.generation,
                        ).values(visible=False))
                    elif plan.kind == "chats":
                        # A live search can discover a chat after this snapshot
                        # began, including while its first page was in flight.
                        # An older absence cannot erase that newer evidence.
                        snapshot_started = datetime.fromisoformat(plan.parameters["snapshot_started_at"])
                        db.execute(update(Chat).where(
                            Chat.generation != plan.generation,
                            Chat.synced_at <= snapshot_started,
                        ).values(visible=False))
                state.synced_at = utcnow()
            db.add(SyncRun(
                id=str(uuid4()), resource=plan.resource,
                status="partial" if has_more else "success", items=len(rows),
                started_at=plan.started, finished_at=utcnow(),
            ))
            result = {"items": len(rows), "has_more": has_more, "resource": plan.resource}
            if plan.kind != "instance":
                result["parameters"] = plan.parameters
            return result

    def _store(self, db, kind, raw, identifier, chat_id, generation, *, read_started=None):
        if not isinstance(raw, dict):
            raise FeishuError("invalid_response")
        key = {"chats": "chat_id", "approvals": "task_id"}.get(kind, "message_id")
        if not raw.get(key) or (kind == "approvals" and not raw.get("instance_code")):
            raise FeishuError("invalid_response")
        common = {"synced_at": utcnow(), "raw": raw}
        if kind == "chats":
            upsert(
                db,
                Chat,
                {
                    **common,
                    "chat_id": raw["chat_id"],
                    "name": raw.get("name") or raw["chat_id"],
                    "chat_mode": raw.get("chat_mode", "group"),
                    "generation": generation,
                    "visible": True,
                },
            )
        elif kind == "approvals":
            upsert(
                db,
                ApprovalTask,
                {
                    **common,
                    "task_id": raw["task_id"],
                    "topic": identifier,
                    "instance_code": raw["instance_code"],
                    "title": raw.get("title") or raw.get("definition_name", "审批"),
                    "status": str(raw.get("status", "")),
                    "initiator_name": raw.get("initiator_name") or raw.get("initiator", ""),
                    "generation": generation,
                    "visible": True,
                },
            )
        else:
            sender = raw.get("sender") or {}
            deleted = bool(raw.get("deleted", False))
            cached = db.get(Message, raw["message_id"])
            if cached and not deleted:
                incoming_update = message_activity_time({"update_time": raw.get("update_time")})
                cached_update = message_activity_time({"update_time": cached.raw.get("update_time")})
                if (
                    cached.deleted or cached_update > incoming_update
                    or (
                        read_started is not None and cached.synced_at > read_started
                        and (not incoming_update or not cached_update or incoming_update <= cached_update)
                    )
                ):
                    # Live/manual readers can finish in either order. Preserve
                    # newer edits and permanent recalls regardless of who wins.
                    return
            try:
                created = int(raw.get("create_time") or 0)
            except (ValueError, TypeError):
                raise FeishuError("invalid_response") from None
            # Remove cached content when a re-fetched message is recalled.
            if deleted:
                common["raw"] = {**raw, "body": {"content": ""}}
            upsert(
                db,
                Message,
                {
                    **common,
                    "message_id": raw["message_id"],
                    "chat_id": chat_id,
                    "thread_id": raw.get("thread_id") or (identifier if kind == "thread" else None),
                    "parent_id": raw.get("parent_id"),
                    "msg_type": raw.get("msg_type", "unknown"),
                    "sender_name": sender.get("sender_name")
                    or sender.get("name")
                    or sender.get("id", "系统"),
                    "text": message_text(raw),
                    "create_time": created,
                    "deleted": deleted,
                },
            )


def instance_view(instance: ApprovalInstance) -> dict:
    return {
        "instance_code": instance.instance_code,
        "synced_at": instance.synced_at,
        "data": {**instance.raw, "form": parse_json(instance.raw.get("form", "[]"))},
    }
