"""Cache DMS detail summaries independently of notification and phone delivery."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dms_reviews",
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("owner_open_id", sa.String(128), nullable=False),
        sa.Column("source_message_id", sa.String(128), nullable=False),
        sa.Column("task_key", sa.String(200), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dms_reviews_owner_open_id", "dms_reviews", ["owner_open_id"])
    op.create_index("ix_dms_reviews_next_attempt_at", "dms_reviews", ["next_attempt_at"])


def downgrade():
    op.drop_table("dms_reviews")
