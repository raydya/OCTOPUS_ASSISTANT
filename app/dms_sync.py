"""Read only recent cards from the configured DMS app and personal chat."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import update

from app.dms_attention import _parse, _timestamp
from app.errors import AppError, FeishuError
from app.models import Account, Chat, SyncRun, SyncState, utcnow
from app.sync import _owner, _state_version, message_activity_time

RESOURCE = "dms_cards"
LOOKBACK = timedelta(minutes=15)


def _chat_version(chat):
    return (chat.visible, chat.chat_mode, chat.generation, chat.synced_at) if chat else None


@dataclass(frozen=True)
class DmsWindow:
    owner: tuple
    previous: dict | None
    generation: str
    parameters: dict
    cursor: str | None
    started: datetime
    chat_version: tuple | None


class DmsCardSync:
    def __init__(self, settings, sessions, feishu, sync):
        self.settings, self.sessions, self.feishu, self.sync = settings, sessions, feishu, sync

    def _prepare(self):
        if not self.settings.dms_chat_id or not self.settings.dms_sender_app_id:
            raise AppError("dms_not_configured", "尚未配置可信 DMS 应用及私聊。", 409)
        now = utcnow()
        with self.sessions() as db:
            owner = _owner(db.get(Account, 1))
            if not all(owner):
                raise AppError("dms_owner_required", "请先绑定飞书账号。", 401)
            chat = db.get(Chat, self.settings.dms_chat_id)
            if chat and chat.chat_mode != "p2p":
                raise AppError("dms_chat_unverified", "DMS 来源必须是已核实的私聊。", 409)
            state = db.get(SyncState, RESOURCE)
            previous = _state_version(state)
            parameters = deepcopy(state.parameters) if state else {}
            if parameters and (
                parameters.get("owner_open_id") != owner[0] or parameters.get("tenant_key") != owner[1]
            ):
                raise AppError("dms_owner_changed", "DMS 同步绑定的账号发生变化。", 409)
            current = int(now.timestamp())
            source_changed = parameters and (
                parameters.get("chat_id") != self.settings.dms_chat_id
                or parameters.get("sender_app_id") != self.settings.dms_sender_app_id
            )
            expired = bool(state and state.has_more and int(parameters["end_time"]) < current - 900)
            if state is None or not state.has_more or source_changed or expired:
                gap = parameters.get("last_gap")
                if expired:
                    gap = {"start_time": parameters["start_time"], "end_time": str(current - 900)}
                parameters = {
                    "container_id_type": "chat", "chat_id": self.settings.dms_chat_id,
                    "sender_app_id": self.settings.dms_sender_app_id,
                    "owner_open_id": owner[0], "tenant_key": owner[1],
                    "start_time": str(current - int(LOOKBACK.total_seconds())), "end_time": str(current),
                    "seen_tokens": [], **({"last_gap": gap} if gap else {}),
                }
                generation, cursor = str(uuid4()), None
            else:
                generation, cursor = state.generation, state.page_token
            return DmsWindow(owner, previous, generation, parameters, cursor, now, _chat_version(chat))

    @staticmethod
    def _trusted_card(raw, window):
        if not isinstance(raw, dict):
            return False
        sender = raw.get("sender")
        created = _timestamp(raw.get("create_time"))
        parameters = window.parameters
        return bool(
            raw.get("chat_id") == parameters["chat_id"] and raw.get("chat_type") == "p2p"
            and raw.get("msg_type") == "interactive"
            and isinstance(raw.get("message_id"), str) and raw["message_id"].startswith("om_")
            and isinstance(sender, dict) and sender.get("id") == parameters["sender_app_id"]
            and sender.get("id_type") == "app_id" and sender.get("sender_type") == "app"
            and sender.get("tenant_key") == window.owner[1]
            and created is not None
            and int(parameters["start_time"]) * 1000 <= created <= int(parameters["end_time"]) * 1000
            and _parse(raw) is not None
        )

    def _read(self, window):
        parameters = window.parameters
        cursor, more, accepted = window.cursor, True, []
        tokens = set(parameters["seen_tokens"]) | {cursor}
        new_tokens = []
        # Pass only the actual API's window fields, not checkpoint metadata.
        query = {key: parameters[key] for key in ("container_id_type", "start_time", "end_time")}
        for _ in range(self.settings.sync_page_limit):
            data = self.feishu.messages(parameters["chat_id"], query, cursor)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise FeishuError("invalid_dms_response")
            accepted.extend(raw for raw in data["items"] if self._trusted_card(raw, window))
            more, cursor = data.get("has_more", False), data.get("page_token")
            if type(more) is not bool or (more and (
                not isinstance(cursor, str) or not cursor or cursor in tokens
            )):
                raise AppError("invalid_dms_pagination", "DMS 卡片分页标记重复或缺失。", 502)
            if not more:
                cursor = None
                break
            tokens.add(cursor)
            new_tokens.append(cursor)
        metadata = None
        if accepted and (window.chat_version is None or not window.chat_version[0]):
            metadata = self.feishu.chat(parameters["chat_id"])
            if (
                not isinstance(metadata, dict) or metadata.get("chat_id") != parameters["chat_id"]
                or metadata.get("chat_mode") != "p2p"
            ):
                raise AppError("dms_chat_unverified", "无法以本人身份核实 DMS 私聊。", 409)
        return accepted, more, cursor, new_tokens, metadata

    def _commit(self, window, rows, more, cursor, tokens, metadata):
        with self.sessions.begin() as db:
            self.sync._write_lock(db)
            parameters = deepcopy(window.parameters)
            if (
                _owner(db.get(Account, 1)) != window.owner
                or parameters["chat_id"] != self.settings.dms_chat_id
                or parameters["sender_app_id"] != self.settings.dms_sender_app_id
            ):
                raise AppError("dms_owner_changed", "DMS 同步绑定账号或来源发生变化。", 409)
            state = db.get(SyncState, RESOURCE)
            if _state_version(state) != window.previous:
                raise AppError("sync_cursor_changed", "DMS 同步进度已更新。", 409)
            chat = db.get(Chat, parameters["chat_id"])
            if rows:
                if _chat_version(chat) != window.chat_version:
                    raise AppError("dms_chat_changed", "DMS 私聊状态已变化，等待重新核验。", 409)
                if not chat or not chat.visible:
                    if metadata is None:
                        raise AppError("dms_chat_unverified", "DMS 私聊尚未核实。", 409)
                    directory = db.get(SyncState, "chats")
                    generation = directory.generation if directory and directory.has_more else window.generation
                    self.sync._store(db, "chats", metadata, parameters["chat_id"], parameters["chat_id"], generation)
                for raw in rows:
                    self.sync._store(
                        db, "messages", raw, parameters["chat_id"], parameters["chat_id"], window.generation,
                        read_started=window.started,
                    )
                latest = max(message_activity_time(raw) for raw in rows)
                db.execute(update(Chat).where(
                    Chat.chat_id == parameters["chat_id"],
                    (Chat.last_activity_time.is_(None)) | (Chat.last_activity_time < latest),
                ).values(last_activity_time=latest))
            if state is None:
                state = SyncState(resource=RESOURCE)
                db.add(state)
            parameters["seen_tokens"].extend(tokens)
            state.parameters, state.generation = parameters, window.generation
            state.page_token, state.has_more, state.synced_at = cursor, more, utcnow()
            if not more:
                state.completed_at = utcnow()
            db.add(SyncRun(
                id=str(uuid4()), resource=RESOURCE, status="partial" if more else "success",
                items=len(rows), started_at=window.started, finished_at=utcnow(),
            ))
            return {"resource": RESOURCE, "items": len(rows), "has_more": more, "parameters": parameters}

    def run(self):
        window = self._prepare()
        return self._commit(window, *self._read(window))
