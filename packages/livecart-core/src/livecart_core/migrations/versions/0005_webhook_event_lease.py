"""Lease the payment events a worker claims from the registry.

The workers poll ``processed_webhook_events`` every 2 s and claim at most 20
unprocessed events at a time, ``FOR UPDATE SKIP LOCKED``, each claim counting
an attempt (``attempts``) before the event is applied, in a unit of its own.
The row lock ends with the claim's transaction, so ``claimed_until`` keeps
the claim: until then, another worker's claim skips the event, and an attempt
that failed is tried again once it has passed. NULL until the first claim.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processed_webhook_events",
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processed_webhook_events", "claimed_until")
