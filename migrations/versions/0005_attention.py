"""Persist important mentions and at-most-once telephone escalation markers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_messages_synced_at_message_id", "messages", ["synced_at", "message_id"])
    op.create_table(
        "attention_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discovery_message_id", sa.String(128), nullable=False),
        sa.Column("last_call_submitted_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "message_attention",
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("chat_id", sa.String(128), nullable=False),
        sa.Column("chat_name", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("context_hash", sa.String(64)),
        sa.Column("assessment_context", postgresql.JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("model_result", postgresql.JSONB(), nullable=False),
        sa.Column("assessment_cache", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("assessed_at", sa.DateTime(timezone=True)),
        sa.Column("reminder_uuid", sa.String(36)),
        sa.Column("reminder_message_id", sa.String(128)),
        sa.Column("reminder_submitted_at", sa.DateTime(timezone=True)),
        sa.Column("phone_submitted_at", sa.DateTime(timezone=True)),
        sa.Column("phone_accepted_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_attention_due_at", "message_attention", ["due_at"])
    op.create_index("ix_message_attention_status", "message_attention", ["status"])


def downgrade():
    op.drop_table("message_attention")
    op.drop_table("attention_states")
    op.drop_index("ix_messages_synced_at_message_id", table_name="messages")
