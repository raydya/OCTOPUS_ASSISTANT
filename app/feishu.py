import re
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.errors import AppError, FeishuError
from app.models import Account, utcnow

API_BASE = "https://open.feishu.cn/open-apis"
DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
TOKEN_PATH = "https://accounts.feishu.cn/oauth/v3/token"


class FeishuClient:
    """User data, subscriptions and narrowly scoped approval submission."""

    def __init__(self, settings, security, sessions, transport=None, sleep=time.sleep):
        self.settings = settings
        self.security = security
        self.sessions = sessions
        self.sleep = sleep
        self.http = httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.http.close()

    def start_device_authorization(self) -> dict:
        if not self.settings.feishu_configured:
            raise AppError("not_configured", "请先在 .env 填写飞书 App ID 和 App Secret。", 409)
        data = self._request(
            "POST",
            DEVICE_URL,
            auth=httpx.BasicAuth(
                self.settings.feishu_app_id, self.settings.feishu_app_secret.get_secret_value()
            ),
            data={"client_id": self.settings.feishu_app_id, "scope": self.settings.feishu_scopes},
        )
        try:
            url = data.get("verification_uri_complete") or data["verification_uri"]
            parsed = urlsplit(url)
            if (
                not data["device_code"]
                or not data["user_code"]
                or int(data["expires_in"]) <= 0
                or int(data.get("interval", 5)) <= 0
                or parsed.scheme != "https"
                or parsed.hostname != "accounts.feishu.cn"
                or parsed.username
                or parsed.port not in {None, 443}
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise FeishuError("invalid_device_response") from None
        return {**data, "verification_url": url}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.RequestError:
                # GET is idempotent; never replay an ambiguous OAuth exchange.
                if method == "GET" and attempt < 2:
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise FeishuError("network") from None
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            code = str(body.get("code", "0"))
            retryable = (
                response.status_code == 429 or response.status_code >= 500 or code == "99991400"
            )
            if method == "GET" and retryable and attempt < 2:
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    delay = 2**attempt
                self.sleep(max(0, min(delay, 5)))
                continue
            if response.is_error or response.is_redirect or code != "0" or body.get("error"):
                error = str(body.get("error") or (code if code != "0" else response.status_code))
                # Keep error identifiers bounded and prevent provider text from leaking.
                error = error if re.fullmatch(r"[A-Za-z0-9_]{1,40}", error) else "upstream"
                raise FeishuError(error)
            if not body:
                raise FeishuError("invalid_response")
            return body
        raise FeishuError("network")

    def _token(self, **grant) -> dict:
        token = self._request(
            "POST",
            TOKEN_PATH,
            json={
                "client_id": self.settings.feishu_app_id,
                "client_secret": self.settings.feishu_app_secret.get_secret_value(),
                **grant,
            },
        )
        try:
            if not token.get("access_token") or int(token.get("expires_in", 0)) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            raise FeishuError("invalid_token_response") from None
        return token

    def poll_device_token(self, device_code: str) -> dict:
        return self._token(
            grant_type="urn:ietf:params:oauth:grant-type:device_code",
            device_code=device_code,
        )

    def profile(self, token: str) -> dict:
        body = self._request(
            "GET", "/authen/v1/user_info", headers={"Authorization": f"Bearer {token}"}
        )
        return body.get("data", {})

    def access_token(self, rejected_token: str | None = None) -> str:
        # A separate short transaction persists rotating refresh tokens even if
        # the subsequent data sync fails. Row locking prevents concurrent refresh.
        with self.sessions.begin() as db:
            account = db.scalar(select(Account).where(Account.id == 1).with_for_update())
            if not account:
                raise AppError("connect_required", "请先连接飞书账号。", 409)
            current = self.security.decrypt(account.access_token)
            if account.expires_at > utcnow() + timedelta(seconds=120) and current != rejected_token:
                return current
            if not account.refresh_token or (
                account.refresh_expires_at and account.refresh_expires_at <= utcnow()
            ):
                raise AppError("reauthorize", "飞书授权已过期，请重新连接账号。", 409)
            token = self._token(
                grant_type="refresh_token",
                refresh_token=self.security.decrypt(account.refresh_token),
            )
            self.security.apply_token(account, token)
            return token["access_token"]

    def _get(self, path: str, params: dict) -> dict:
        token = self.access_token()
        for attempt in range(2):
            try:
                body = self._request(
                    "GET", path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                data = body.get("data")
                if not isinstance(data, dict):
                    raise FeishuError("invalid_response")
                return data
            except FeishuError as exc:
                if attempt == 0 and exc.provider_code in {
                    "401",
                    "99991663",
                    "99991668",
                    "99991677",
                }:
                    token = self.access_token(rejected_token=token)
                    continue
                raise
        raise FeishuError("upstream")

    def app_token(self) -> str:
        return self._token(grant_type="client_credentials")["access_token"]

    def online_connections(self) -> int:
        body = self._request(
            "GET", "/event/v1/connection", headers={"Authorization": f"Bearer {self.app_token()}"}
        )
        count = body.get("data", {}).get("online_instance_cnt")
        if not isinstance(count, int) or count < 0:
            raise FeishuError("invalid_response")
        return count

    def subscribe_approvals(self):
        # Register only approvals involving this user, not all managed definitions.
        for kind in ("instances", "tasks"):
            self._request(
                "POST",
                f"/approval/v4/{kind}/subscription",
                headers={"Authorization": f"Bearer {self.access_token()}"},
                json={"subscription_type": "INVOLVED_APPROVAL"},
            )

    def chats(self, page_token: str | None) -> dict:
        return self._get(
            "/im/v1/chats",
            {
                "types": "p2p,group",
                "user_id_type": "open_id",
                "page_size": 100,
                **({"page_token": page_token} if page_token else {}),
            },
        )

    def messages(self, container_id: str, parameters: dict, page_token: str | None) -> dict:
        return self._get(
            "/im/v1/messages",
            {
                "container_id": container_id,
                "page_size": 50,
                "sort_type": "ByCreateTimeDesc",
                "with_sender_name": "true",
                **parameters,
                **({"page_token": page_token} if page_token else {}),
            },
        )

    def tasks(self, topic: str, page_token: str | None) -> dict:
        # User-identity endpoint; /tasks/query is a different, legacy contract.
        return self._get(
            "/approval/v4/tasks",
            {
                "topic": topic,
                "locale": "zh-CN",
                "user_id_type": "open_id",
                "page_size": 100,
                **({"page_token": page_token} if page_token else {}),
            },
        )

    def instance(self, instance_code: str) -> dict:
        return self._get(
            "/approval/v4/instances/detail",
            {"instance_code": instance_code, "locale": "zh-CN", "user_id_type": "open_id"},
        )

    def approve_task(self, instance_code: str, task_id: str, comment: str):
        # Refresh before sending. Never retry a write, including an ambiguous
        # timeout: the reviewer persists submitting before this method is called.
        token = self.access_token()
        with self.sessions() as db:
            account = db.get(Account, 1)
            if not account or "approval:task:write" not in account.scope.split():
                raise AppError(
                    "approval_write_scope_required",
                    "请开通 approval:task:write 并重新授权后再提交审批。",
                    409,
                )
        # New user-identity API, distinct from legacy /tasks/approve.
        return self._request(
            "POST",
            "/approval/v4/tasks/pass",
            headers={"Authorization": f"Bearer {token}"},
            json={"instance_code": instance_code, "task_id": task_id, "comment": comment},
        )
