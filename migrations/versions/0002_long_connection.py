"""Device authorization and durable long-connection synchronization."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_oauth_attempts_expires_at", table_name="oauth_attempts")
    op.drop_table("oauth_attempts")
    op.create_table(
        "device_authorizations",
        sa.Column("session_hash", sa.String(64), primary_key=True),
        sa.Column("device_code", sa.Text(), nullable=True),
        sa.Column("exchanged_token", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_authorizations_expires_at", "device_authorizations", ["expires_at"])
    op.create_table(
        "event_inbox",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("hints", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_event_inbox_processed_at", "event_inbox", ["processed_at"])
    op.create_table(
        "sync_jobs",
        sa.Column("resource", sa.String(300), primary_key=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
    )
    op.create_index("ix_sync_jobs_next_attempt_at", "sync_jobs", ["next_attempt_at"])
    op.create_table(
        "worker_states",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
    )


def downgrade():
    for name in ("worker_states", "sync_jobs", "event_inbox", "device_authorizations"):
        op.drop_table(name)
    op.create_table(
        "oauth_attempts",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("verifier", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oauth_attempts_expires_at", "oauth_attempts", ["expires_at"])
