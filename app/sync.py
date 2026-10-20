import time
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.content import message_text, parse_json
from app.errors import AppError, FeishuError
from app.models import ApprovalInstance, ApprovalTask, Chat, Message, SyncRun, SyncState, utcnow


def upsert(db, model, values):
    keys = [column.name for column in model.__table__.primary_key]
    stmt = insert(model).values(**values)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=keys,
            set_={key: value for key, value in values.items() if key not in keys},
        )
    )


class SyncService:
    def __init__(self, settings, sessions, feishu):
        self.settings = settings
        self.sessions = sessions
        self.feishu = feishu

    def run(self, resource: str, *, days=7, restart=False) -> dict:
        started = utcnow()
        try:
            with self.sessions.begin() as db:
                if not db.scalar(text("SELECT pg_try_advisory_xact_lock(81024002)")):
                    raise AppError("sync_busy", "已有同步正在进行，请稍后重试。", 409)
                result = self._run(db, resource, days, restart)
                db.add(
                    SyncRun(
                        id=str(uuid4()),
                        resource=resource,
                        status="partial" if result["has_more"] else "success",
                        items=result["items"],
                        started_at=started,
                        finished_at=utcnow(),
                    )
                )
                return result
        except AppError as exc:
            if exc.code != "sync_busy":
                with self.sessions.begin() as db:
                    db.add(
                        SyncRun(
                            id=str(uuid4()),
                            resource=resource,
                            status="failed",
                            items=0,
                            error_code=exc.code,
                            started_at=started,
                            finished_at=utcnow(),
                        )
                    )
            raise

    def _run(self, db, resource: str, days: int, restart: bool) -> dict:
        kind, _, identifier = resource.partition(":")
        chat_id = identifier
        if kind == "messages":
            if not db.get(Chat, identifier):
                raise AppError("chat_not_found", "请先同步会话列表。", 404)
        if kind == "thread":
            parent = db.scalar(select(Message).where(Message.thread_id == identifier).limit(1))
            if not parent:
                raise AppError("thread_not_found", "请先同步包含该话题的会话。", 404)
            chat_id = parent.chat_id
        if kind == "instance":
            known = db.scalar(
                select(ApprovalTask).where(ApprovalTask.instance_code == identifier).limit(1)
            )
            if not known:
                raise AppError("instance_not_found", "请先同步对应审批任务。", 404)
            raw = self.feishu.instance(identifier)
            upsert(
                db,
                ApprovalInstance,
                {"instance_code": identifier, "raw": raw, "synced_at": utcnow()},
            )
            return {"items": 1, "has_more": False, "resource": resource}
        if kind not in {"chats", "messages", "thread", "approvals"}:
            raise AppError("invalid_resource", "不支持的同步类型。")
        state = db.get(SyncState, resource)
        if state is None:
            state = SyncState(resource=resource)
            db.add(state)
        if restart or not state.has_more:
            state.generation = str(uuid4())
            state.page_token = None
            state.parameters = {}
            if kind == "messages":
                now = int(time.time())
                state.parameters = {
                    "container_id_type": "chat",
                    "start_time": str(now - days * 86400),
                    "end_time": str(now),
                }
            elif kind == "thread":
                state.parameters = {"container_id_type": "thread"}
        total = 0
        seen_tokens = {state.page_token}
        for _ in range(self.settings.sync_page_limit):
            if kind == "chats":
                data = self.feishu.chats(state.page_token)
            elif kind == "approvals":
                data = self.feishu.tasks(identifier, state.page_token)
            else:
                data = self.feishu.messages(identifier, state.parameters, state.page_token)
            rows = data.get("tasks" if kind == "approvals" else "items")
            if not isinstance(rows, list):
                raise FeishuError("invalid_response")
            for raw in rows:
                self._store(db, kind, raw, identifier, chat_id, state.generation)
            total += len(rows)
            state.has_more = bool(data.get("has_more", False))
            state.page_token = data.get("page_token") if state.has_more else None
            if state.has_more and (not state.page_token or state.page_token in seen_tokens):
                raise AppError(
                    "invalid_pagination",
                    "飞书返回了重复或缺失的分页标记，本批数据未提交。请重试或重新同步。",
                    502,
                )
            seen_tokens.add(state.page_token)
            if not state.has_more:
                state.completed_at = utcnow()
                # Reconcile membership only after the entire snapshot succeeds.
                # Disappearing from pending does NOT imply approval succeeded.
                if kind == "approvals":
                    db.execute(
                        update(ApprovalTask)
                        .where(
                            ApprovalTask.topic == identifier,
                            ApprovalTask.generation != state.generation,
                        )
                        .values(visible=False)
                    )
                elif kind == "chats":
                    db.execute(
                        update(Chat)
                        .where(Chat.generation != state.generation)
                        .values(visible=False)
                    )
                break
        state.synced_at = utcnow()
        return {
            "items": total,
            "has_more": state.has_more,
            "resource": resource,
            "parameters": state.parameters,
        }

    def _store(self, db, kind, raw, identifier, chat_id, generation):
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
