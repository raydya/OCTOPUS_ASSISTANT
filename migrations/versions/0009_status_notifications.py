"""Durable owner-only status notifications, separate from phone escalation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "status_notification_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_message_id", sa.String(128), nullable=False),
    )
    op.create_table(
        "status_notice_sources",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("notify_changes", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_open_id", "source_type", "source_id"),
    )
    op.create_table(
        "status_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("message_id", sa.String(128)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_key", "revision"),
    )
    op.create_index("ix_status_notifications_source_key", "status_notifications", ["source_key"])
    op.create_index("ix_status_notifications_status", "status_notifications", ["status"])


def downgrade():
    op.drop_table("status_notifications")
    op.drop_table("status_notice_sources")
    op.drop_table("status_notification_states")
