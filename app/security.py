import hashlib
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy import delete, text

from app.errors import AppError
from app.models import Account, BrowserSession, DeviceAuthorization, utcnow

COOKIE_NAME = "octopus_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Security:
    def __init__(self, settings, sessions):
        self.settings = settings
        self.sessions = sessions
        self.cipher = Fernet(settings.token_encryption_key.get_secret_value().encode())

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def login(self, password: str) -> str:
        if not secrets.compare_digest(
            password.encode(), self.settings.app_password.get_secret_value().encode()
        ):
            raise AppError("invalid_password", "工作台密码不正确。", 401)
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as db:
            db.execute(delete(BrowserSession).where(BrowserSession.expires_at < utcnow()))
            db.execute(delete(DeviceAuthorization).where(DeviceAuthorization.expires_at < utcnow()))
            db.add(
                BrowserSession(
                    token_hash=digest(token),
                    csrf_token=secrets.token_urlsafe(32),
                    expires_at=utcnow() + timedelta(hours=self.settings.session_hours),
                )
            )
        return token

    def session(self, token: str | None) -> BrowserSession:
        if not token or len(token) > 128:
            raise AppError("login_required", "请先登录工作台。", 401)
        with self.sessions() as db:
            session = db.get(BrowserSession, digest(token))
            if not session or session.expires_at <= utcnow():
                raise AppError("login_required", "登录已过期，请重新登录工作台。", 401)
            return session

    def save_account(self, profile: dict, token: dict):
        open_id = profile.get("open_id")
        if not open_id:
            raise AppError("invalid_profile", "飞书未返回账号标识。", 502)
        allowed = self.settings.feishu_allowed_open_id
        if allowed and allowed != open_id:
            raise AppError("wrong_account", "该飞书账号不在允许范围内。", 403)
        with self.sessions.begin() as db:
            # Serializes first-time binding too, when no account row exists yet.
            db.execute(text("SELECT pg_advisory_xact_lock(81024001)"))
            account = db.get(Account, 1)
            if account and (
                account.open_id != open_id or account.tenant_key != profile.get("tenant_key", "")
            ):
                raise AppError(
                    "wrong_account", "工作台已绑定其他飞书账号，请使用原账号重新授权。", 403
                )
            if not account:
                account = Account(id=1, open_id=open_id)
                db.add(account)
            account.name = profile.get("name") or open_id
            account.tenant_key = profile.get("tenant_key", "")
            self.apply_token(account, token)

    def apply_token(self, account: Account, token: dict):
        account.access_token = self.encrypt(token["access_token"])
        account.expires_at = utcnow() + timedelta(seconds=int(token["expires_in"]))
        # OAuth reauthorization and refresh responses are authoritative. No old
        # refresh token survives when the provider stops returning one.
        account.refresh_token = (
            self.encrypt(token["refresh_token"]) if token.get("refresh_token") else None
        )
        refresh_lifetime = token.get("refresh_token_expires_in")
        account.refresh_expires_at = (
            utcnow() + timedelta(seconds=int(refresh_lifetime)) if refresh_lifetime else None
        )
        account.scope = token.get("scope", account.scope or "")
