from alembic import context
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db import make_engine, wait_for_database
from app.models import Base


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    migration_database_url: SecretStr
    database_wait_seconds: float = Field(default=60, ge=1, le=300)

    @field_validator("migration_database_url")
    @classmethod
    def validate_url(cls, value):
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("MIGRATION_DATABASE_URL must use postgresql+psycopg://")
        return value


settings = MigrationSettings()
engine = make_engine(settings.migration_database_url.get_secret_value())
try:
    wait_for_database(engine, timeout=settings.database_wait_seconds, require_schema=False)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
