import os
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.models import Base, utcnow


@pytest.fixture
def settings(tmp_path):
    policy = tmp_path / "approval-policy.json"
    policy.write_text('{"version":"test-empty","rules":[]}')
    return Settings(
        _env_file=None,
        database_url=os.environ.get(
            "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused_test"
        ),
        app_password="local-test-password-123456",
        token_encryption_key=Fernet.generate_key().decode(),
        feishu_app_id="cli_test",
        feishu_app_secret="test-app-secret",
        sync_page_limit=1,
        reminders_only=False,
        app_base_url="http://localhost:8000",
        approval_policy_path=str(policy),
        approval_model_enabled=False,
        attention_enabled=False,
    )


@pytest.fixture
def database(settings, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Refusing to reset a database whose name does not end with _test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    monkeypatch.setenv("APP_PASSWORD", settings.app_password.get_secret_value())
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", settings.token_encryption_key.get_secret_value())
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    with engine.begin() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
    yield engine
    engine.dispose()


@pytest.fixture
def make_app(settings, database):
    apps = []

    def factory(handler, connected=True):
        app = create_app(settings, transport=httpx.MockTransport(handler))
        app.state.feishu.sleep = lambda _: None
        if connected:
            app.state.security.save_account(
                {"open_id": "ou_owner", "name": "测试用户", "tenant_key": "tenant_test"},
                {
                    "access_token": "user-access",
                    "refresh_token": "user-refresh",
                    "expires_in": 7200,
                    "refresh_token_expires_in": 604800,
                    "scope": settings.feishu_scopes,
                },
            )
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.state.feishu.close()


def token_response(access="user-access", refresh="user-refresh"):
    return {
        "code": 0,
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 7200,
        "refresh_token_expires_in": 604800,
        "scope": "offline_access",
    }


def ok(data):
    return httpx.Response(200, json={"code": 0, "data": data})


def login(client):
    response = client.post("/auth/login", json={"password": "local-test-password-123456"})
    assert response.status_code == 200
    status = client.get("/api/status").json()
    return {"X-CSRF-Token": status["csrf_token"], "Origin": "http://localhost:8000"}


def expire_access(app):
    from app.models import Account

    with app.state.sessions.begin() as db:
        db.get(Account, 1).expires_at = utcnow() - timedelta(seconds=1)
