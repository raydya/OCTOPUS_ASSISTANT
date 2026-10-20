from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    database_wait_seconds: float = Field(default=60, ge=1, le=300)
    app_base_url: str = "http://localhost:8000"
    app_password: SecretStr
    token_encryption_key: SecretStr
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_allowed_open_id: str = ""
    feishu_scopes: str = (
        "offline_access contact:user.base:readonly im:chat:read im:message:readonly "
        "im:message.p2p_msg:get_as_user im:message.group_msg:get_as_user "
        "approval:task:read approval:instance:read approval:task:write"
    )
    sync_page_limit: int = Field(default=5, ge=1, le=20)
    http_timeout_seconds: float = Field(default=15, ge=1, le=60)
    session_hours: int = Field(default=24, ge=1, le=168)
    sync_interval_seconds: int = Field(default=60, ge=30, le=3600)
    approval_automation_enabled: bool = True
    approval_policy_path: str = "approval-policy.json"

    @model_validator(mode="after")
    def validate_settings(self):
        if len(self.app_password.get_secret_value()) < 16:
            raise ValueError("APP_PASSWORD 至少 16 个字符；请运行 scripts/setup_env.py")
        try:
            Fernet(self.token_encryption_key.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY 必须是有效的 Fernet 密钥") from exc
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL 必须使用 postgresql+psycopg://")
        self.app_base_url = self.app_base_url.rstrip("/")
        url = urlsplit(self.app_base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.path
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("APP_BASE_URL 必须是 http(s) origin，不包含路径或凭证")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            raise ValueError("非本机部署必须使用 HTTPS")
        return self

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret.get_secret_value())

    @property
    def secure_cookie(self) -> bool:
        return self.app_base_url.startswith("https://")
