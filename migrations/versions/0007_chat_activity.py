"""Order conversations by the latest source message activity."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chats", sa.Column("last_activity_time", sa.BigInteger(), nullable=True))
    # Match the source timestamp validation in sync.message_activity_time.
    # Guard the JSON cast separately so malformed or oversized values stay harmless.
    op.execute(
        """
        UPDATE chats AS c
        SET last_activity_time = activity.latest
        FROM (
            SELECT chat_id, NULLIF(MAX(GREATEST(
                CASE WHEN create_time BETWEEN 1 AND 253402300799999
                    THEN create_time ELSE 0 END,
                CASE WHEN raw->>'update_time' ~ '^[0-9]{1,15}$'
                    THEN CASE WHEN (raw->>'update_time')::bigint
                                   BETWEEN 1 AND 253402300799999
                        THEN (raw->>'update_time')::bigint ELSE 0 END
                    ELSE 0 END
            )), 0) AS latest
            FROM messages
            GROUP BY chat_id
        ) AS activity
        WHERE c.chat_id = activity.chat_id
        """
    )
    op.create_index(
        "ix_chats_activity_order",
        "chats",
        [sa.text("last_activity_time DESC NULLS LAST"), "chat_id"],
    )


def downgrade():
    op.drop_index("ix_chats_activity_order", table_name="chats")
    op.drop_column("chats", "last_activity_time")
