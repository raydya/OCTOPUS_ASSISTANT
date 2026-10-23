"""Read recent personal mentions independently of the historical sync queue.

Search and metadata requests run outside database transactions. A page's cache
updates and cursor are committed together after checking the bound identity and
the previously read cursor. This reader never sends notifications or takes an
approval action; the existing consumers retain their source-age checks.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.attention import is_personal_mention
from app.errors import AppError
from app.models import Account, Chat, Message, SyncState, utcnow
from app.sync import message_activity_time

RESOURCE = "mentions"
REQUIRED_SCOPE = "search:message"
LOOKBACK = timedelta(minutes=15)


@dataclass(frozen=True)
class Window:
    owner: str
    tenant: str
    generation: str
    parameters: dict
    cursor: str | None

    @property
    def start(self):
        return datetime.fromisoformat(self.parameters["start_time"])

    @property
    def end(self):
        return datetime.fromisoformat(self.parameters["end_time"])


class MentionSync:
    def __init__(self, settings, sessions, feishu, sync):
        self.settings, self.sessions, self.feishu, self.sync = settings, sessions, feishu, sync

    def _prepare(self):
        now = utcnow().replace(microsecond=0)
        with self.sessions.begin() as db:
            account = db.get(Account, 1)
            if not account:
                return {"status": "waiting_authorization", "required_scope": REQUIRED_SCOPE}
            if REQUIRED_SCOPE not in account.scope.split():
                return {
                    "status": "blocked_permission", "error_code": "mention_search_scope_required",
                    "required_scope": REQUIRED_SCOPE, "missing_scopes": [REQUIRED_SCOPE],
                }
            db.execute(insert(SyncState).values(
                resource=RESOURCE, generation=str(uuid4()), parameters={}, has_more=False,
            ).on_conflict_do_nothing(index_elements=["resource"]))
            state = db.get(SyncState, RESOURCE, with_for_update=True)
            parameters = deepcopy(state.parameters)
            if parameters and (
                parameters.get("owner_open_id") != account.open_id
                or parameters.get("tenant_key") != account.tenant_key
            ):
                raise AppError("mention_owner_changed", "消息检索绑定的账号发生变化。", 409)
            previous_end = datetime.fromisoformat(parameters["end_time"]) if parameters else None
            expired = state.has_more and previous_end < now - LOOKBACK
            if not state.has_more or expired:
                gap = parameters.get("last_gap")
                if expired:
                    gap = {
                        "start_time": parameters["start_time"], "end_time": (now - LOOKBACK).isoformat(),
                        "reason": "expired_window",
                    }
                elif previous_end and previous_end < now - LOOKBACK:
                    gap = {
                        "start_time": previous_end.isoformat(), "end_time": (now - LOOKBACK).isoformat(),
                        "reason": "reader_offline",
                    }
                parameters = {
                    "owner_open_id": account.open_id, "tenant_key": account.tenant_key,
                    "start_time": (now - LOOKBACK).isoformat(), "end_time": now.isoformat(),
                    "seen_tokens": [],
                    **({"last_gap": gap} if gap else {}),
                    **({"completed_until": state.parameters["completed_until"]}
                       if state.parameters.get("completed_until") else {}),
                }
                state.parameters = parameters
                state.generation, state.page_token, state.has_more = str(uuid4()), None, True
            return Window(
                account.open_id, account.tenant_key, state.generation,
                deepcopy(state.parameters), state.page_token,
            )

    def _read(self, window):
        data = self.feishu.search_mentions(
            window.parameters["start_time"], window.parameters["end_time"], window.cursor,
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise AppError("mention_invalid_search", "消息检索结果格式无效。", 502)
        more, cursor = data.get("has_more", False), data.get("page_token")
        if type(more) is not bool or (
            more and (not isinstance(cursor, str) or not cursor
                      or cursor == window.cursor or cursor in window.parameters["seen_tokens"])
        ):
            raise AppError("mention_invalid_pagination", "消息检索分页标记重复或缺失。", 502)
        metadata = {}
        for hit in data["items"]:
            meta = hit.get("meta_data") if isinstance(hit, dict) else None
            identifier = meta.get("message_id") if isinstance(meta, dict) else None
            if not isinstance(identifier, str) or not identifier.startswith("om_"):
                raise AppError("mention_invalid_search", "消息检索标识无效。", 502)
            if identifier in metadata and metadata[identifier] != meta:
                raise AppError("mention_invalid_search", "消息检索元数据不一致。", 502)
            metadata[identifier] = meta
        if len(metadata) > 30:
            raise AppError("mention_invalid_search", "消息检索超过单页上限。", 502)
        rows = self.feishu.messages_by_ids(list(metadata)) if metadata else []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AppError("mention_invalid_messages", "消息详情结果格式无效。", 502)
        ids = [row.get("message_id") for row in rows]
        if (
            any(not isinstance(identifier, str) for identifier in ids)
            or len(ids) != len(set(ids)) or not set(ids).issubset(metadata)
        ):
            raise AppError("mention_incomplete_messages", "消息检索详情未完整核验。", 502)
        missing = sorted(set(metadata) - set(ids))
        accepted = []
        for raw in rows:
            chat_id = raw.get("chat_id")
            if not isinstance(chat_id, str) or not chat_id.startswith("oc_"):
                raise AppError("mention_invalid_chat", "消息会话标识无效。", 502)
            expected_chat = metadata[raw["message_id"]].get("chat_id")
            if expected_chat and expected_chat != chat_id:
                raise AppError("mention_invalid_chat", "消息与检索会话不一致。", 502)
            if not is_personal_mention(raw, window.owner, self.settings.feishu_app_id):
                continue
            created = datetime.fromtimestamp(int(raw["create_time"]) / 1000, UTC)
            if window.start <= created <= window.end:
                accepted.append(raw)
        chat_ids = {raw["chat_id"] for raw in accepted}
        with self.sessions() as db:
            query = select(Chat.chat_id).where(Chat.chat_id.in_(chat_ids))
            if getattr(self.settings, "reminders_only", True):
                # No directory sweep will revive a previously hidden chat.
                # Revalidate only chats carrying an explicit current mention.
                query = query.where(Chat.visible.is_(True))
            known = set(db.scalars(query))
        new_chats = {}
        for chat_id in sorted(chat_ids - known):
            chat = self.feishu.chat(chat_id)
            if (
                not isinstance(chat, dict) or chat.get("chat_id") != chat_id
                or chat.get("chat_mode") not in {"group", "p2p", "topic"}
                or not isinstance(chat.get("name", ""), str)
            ):
                raise AppError("mention_invalid_chat", "无法核验消息会话元数据。", 502)
            new_chats[chat_id] = chat
        return accepted, new_chats, more, cursor, bool(data.get("notice")), missing

    def _commit(self, window, rows, new_chats, more, cursor, notice, missing, read_started):
        with self.sessions.begin() as db:
            # Coordinate writes with normal/manual sync, never hold this lock
            # during HTTP calls or wait behind a long historical API request.
            if not db.scalar(text("SELECT pg_try_advisory_xact_lock(81024002)")):
                raise AppError("mention_sync_busy", "消息缓存正在更新，稍后继续检索。", 409)
            account = db.get(Account, 1)
            state = db.get(SyncState, RESOURCE, with_for_update=True)
            if (
                not account or account.open_id != window.owner or account.tenant_key != window.tenant
                or REQUIRED_SCOPE not in account.scope.split()
            ):
                raise AppError("mention_owner_changed", "消息检索的绑定账号或授权发生变化。", 409)
            if (
                not state or not state.has_more or state.generation != window.generation
                or state.page_token != window.cursor or state.parameters != window.parameters
            ):
                raise AppError("mention_cursor_changed", "消息检索进度已更新。", 409)
            for raw in rows:
                chat_id = raw["chat_id"]
                cached = db.get(Message, raw["message_id"])
                if cached and not raw.get("deleted"):
                    incoming_update = message_activity_time({"update_time": raw.get("update_time")})
                    cached_update = message_activity_time({"update_time": cached.raw.get("update_time")})
                    if (
                        cached.deleted or cached_update > incoming_update
                        or ((not incoming_update or not cached_update) and cached.synced_at > read_started)
                    ):
                        # A normal/manual sync may have committed a newer edit or
                        # recall while this page's HTTP reads were in flight.
                        continue
                chat = db.get(Chat, chat_id)
                restore = (
                    chat is not None and not chat.visible
                    and getattr(self.settings, "reminders_only", True)
                )
                if chat is None or restore:
                    if chat_id not in new_chats:
                        raise AppError("mention_invalid_chat", "消息会话已失效。", 409)
                    directory = db.get(SyncState, "chats")
                    generation = chat.generation if restore else (
                        directory.generation if directory and directory.has_more else window.generation
                    )
                    self.sync._store(db, "chats", new_chats[chat_id], chat_id, chat_id, generation)
                # Existing visible chats and directory cursors remain untouched.
                self.sync._store(
                    db, "messages", raw, chat_id, chat_id, window.generation,
                    read_started=read_started,
                )
                latest = message_activity_time(raw)
                db.execute(update(Chat).where(
                    Chat.chat_id == chat_id,
                    (Chat.last_activity_time.is_(None)) | (Chat.last_activity_time < latest),
                ).values(last_activity_time=latest))
            state.synced_at = utcnow()
            if notice:
                # A search warning can indicate incomplete visibility/results.
                # Cache verified hits, but retain this page for a later retry.
                return window
            parameters = deepcopy(window.parameters)
            missing_ids = sorted(set(parameters.get("missing_ids", [])) | set(missing))
            if missing_ids:
                # Deleted/inaccessible search hits must not block other messages
                # or later pages. Keep the coverage gap, then retry by overlap.
                parameters["missing_ids"] = missing_ids
                parameters["last_gap"] = {
                    "start_time": parameters["start_time"], "end_time": parameters["end_time"],
                    "reason": "missing_message_details", "missing_count": len(missing_ids),
                }
            if more:
                parameters["seen_tokens"].append(cursor)
            elif not missing_ids:
                parameters["completed_until"] = parameters["end_time"]
                state.completed_at = utcnow()
            state.parameters, state.page_token, state.has_more = parameters, cursor if more else None, more
            return Window(window.owner, window.tenant, window.generation, parameters, state.page_token)

    def run(self):
        window = self._prepare()
        if isinstance(window, dict):
            return window
        items = pages = 0
        more, notice = True, False
        for _ in range(self.settings.sync_page_limit):
            read_started = utcnow()
            rows, chats, more, cursor, notice, missing = self._read(window)
            window = self._commit(window, rows, chats, more, cursor, notice, missing, read_started)
            items += len(rows)
            pages += 1
            if not more or notice:
                break
        missing_count = len(window.parameters.get("missing_ids", []))
        return {
            "status": "partial" if more or notice or missing_count else "synced",
            "error_code": "mention_search_notice" if notice else "mention_messages_partial" if missing_count else None,
            "required_scope": REQUIRED_SCOPE, "missing_scopes": [],
            "items": items, "pages": pages, "has_more": more or notice,
            "window_start": window.parameters["start_time"], "window_end": window.parameters["end_time"],
            "completed_until": window.parameters.get("completed_until"),
            "notice_present": notice,
            "missing_count": missing_count,
            "last_gap": window.parameters.get("last_gap"),
        }
