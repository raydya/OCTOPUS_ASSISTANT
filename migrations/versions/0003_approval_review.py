"""Durable approval decisions and single-attempt execution records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "approval_reviews",
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("instance_code", sa.String(128), nullable=False),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("execution_status", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("rule_id", sa.String(128), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_reviews_instance_code", "approval_reviews", ["instance_code"])
    op.create_index("ix_approval_reviews_reviewed_at", "approval_reviews", ["reviewed_at"])


def downgrade():
    op.drop_table("approval_reviews")
