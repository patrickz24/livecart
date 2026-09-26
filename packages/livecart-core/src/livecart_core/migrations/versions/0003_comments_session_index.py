"""Index the comments by session: every comment of a session, whatever its status.

``ix_comments_rejected_session_id_rejection_reason`` only holds rejected
comments, so a query that reads a session's comments whatever their status
scanned the whole inbox: the reset of the load test session deletes them by
``session_id``, and the ``ON DELETE SET NULL`` of their foreign key looks
them up the same way when a session row goes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_comments_session_id", "comments", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_comments_session_id", table_name="comments")
