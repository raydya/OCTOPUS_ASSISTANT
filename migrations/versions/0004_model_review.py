"""Persist model assessments, their exact inputs' identity, and human feedback."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_reviews", sa.Column("assessment_id", sa.String(36)))
    op.add_column("approval_reviews", sa.Column("snapshot_hash", sa.String(64)))
    op.add_column("approval_reviews", sa.Column("model_review", postgresql.JSONB()))
    op.create_table(
        "model_assessments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cache_key", sa.String(64), nullable=False, unique=True),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("instance_code", sa.String(128), nullable=False),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("feedback_id", sa.String(36)),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("reasoning_effort", sa.String(16), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_assessments_task_id", "model_assessments", ["task_id"])
    op.create_table(
        "approval_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("assessment_id", sa.String(36), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approval_feedback_task_id", "approval_feedback", ["task_id"])


def downgrade():
    op.drop_table("approval_feedback")
    op.drop_table("model_assessments")
    op.drop_column("approval_reviews", "model_review")
    op.drop_column("approval_reviews", "snapshot_hash")
    op.drop_column("approval_reviews", "assessment_id")
