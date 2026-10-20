"""Allow human-intervention approvals to share durable telephone escalation."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "message_attention",
        sa.Column("source_type", sa.String(16), nullable=False, server_default="message"),
    )
    op.add_column("message_attention", sa.Column("approval_task_id", sa.String(128)))
    op.add_column("message_attention", sa.Column("approval_instance_code", sa.String(128)))
    op.add_column("message_attention", sa.Column("approval_assessment_id", sa.String(36)))
    op.create_index(
        "ix_message_attention_approval_task_id", "message_attention", ["approval_task_id"],
    )


def downgrade():
    op.drop_index("ix_message_attention_approval_task_id", table_name="message_attention")
    op.drop_column("message_attention", "approval_assessment_id")
    op.drop_column("message_attention", "approval_instance_code")
    op.drop_column("message_attention", "approval_task_id")
    op.drop_column("message_attention", "source_type")
