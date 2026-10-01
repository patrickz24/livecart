"""Shipments: the carrier's latest event always known, and the lease of their tracking.

A shipment is recorded from the carrier's first answer, and its creation is
its first event: ``last_event_at`` starts at ``carrier_created_at`` and only
moves forward, so it is NOT NULL from now on. No code wrote a shipment before
this revision; a row that exists anyway gets its creation time first.

Every worker polls the carrier about the shipments in flight once a minute,
and leases a batch at a time, ``FOR UPDATE SKIP LOCKED``. The row lock ends
with the lease's transaction, so ``tracking_lease_until`` keeps the lease:
until then, another worker's poll skips the shipment. NULL until its first
poll. ``ix_shipments_in_flight`` holds the shipments neither delivered nor
cancelled, the only ones polled, by id.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)
_IN_FLIGHT = ("created", "in_transit", "out_for_delivery")


def upgrade() -> None:
    shipments = sa.table(
        "shipments",
        sa.column("last_event_at", _TIMESTAMP),
        sa.column("carrier_created_at", _TIMESTAMP),
    )
    op.execute(
        shipments.update()
        .where(shipments.c.last_event_at.is_(None))
        .values(last_event_at=shipments.c.carrier_created_at)
    )
    op.alter_column("shipments", "last_event_at", existing_type=_TIMESTAMP, nullable=False)
    op.add_column("shipments", sa.Column("tracking_lease_until", _TIMESTAMP, nullable=True))
    op.create_index(
        "ix_shipments_in_flight",
        "shipments",
        ["id"],
        postgresql_where=sa.column("status", sa.String).in_(_IN_FLIGHT),
    )


def downgrade() -> None:
    op.drop_index("ix_shipments_in_flight", table_name="shipments")
    op.drop_column("shipments", "tracking_lease_until")
    op.alter_column("shipments", "last_event_at", existing_type=_TIMESTAMP, nullable=True)
