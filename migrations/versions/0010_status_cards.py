"""Anchor one interactive owner-status card per business source."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("status_notice_sources", sa.Column("card_message_id", sa.String(128)))
    op.add_column("status_notice_sources", sa.Column(
        "card_create_state", sa.String(16), nullable=False, server_default="none",
    ))
    op.add_column("status_notice_sources", sa.Column("card_receipt", postgresql.JSONB()))
    op.add_column("status_notice_sources", sa.Column("card_sent_revision", sa.String(64)))
    op.add_column("status_notifications", sa.Column(
        "delivery_action", sa.String(16), nullable=False, server_default="legacy",
    ))
    op.add_column("status_notifications", sa.Column("card_payload", postgresql.JSONB()))
    op.add_column("status_notifications", sa.Column("source_revision", sa.String(64)))
    # Keep historical text and outcomes. Existing unsent text is not replayed as
    # a new card merely because the application was upgraded.
    op.execute(sa.text("""
        UPDATE status_notifications
           SET status='superseded', error_code='status_legacy_migrated'
         WHERE status IN ('queued', 'blocked_permission') AND submitted_at IS NULL
    """))


def downgrade():
    op.drop_column("status_notifications", "source_revision")
    op.drop_column("status_notifications", "card_payload")
    op.drop_column("status_notifications", "delivery_action")
    op.drop_column("status_notice_sources", "card_sent_revision")
    op.drop_column("status_notice_sources", "card_receipt")
    op.drop_column("status_notice_sources", "card_create_state")
    op.drop_column("status_notice_sources", "card_message_id")
