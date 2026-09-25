"""Baseline: the root of the revision history, before any table exists.

Its only statement locks the version table. Alembic creates alembic_version as
livecart_migrator just before it runs this revision, so the table inherits the
default privileges of infra/postgres/grants.sql: SELECT, INSERT and UPDATE for
livecart_app. The runtime role never needs the recorded revision, and writing
it would make the next ``livecart-migrate upgrade head`` skip or re-run
migrations, so every privilege of livecart_app on it is revoked.

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE ALL ON TABLE alembic_version FROM livecart_app")


def downgrade() -> None:
    # alembic_version outlives the downgrade to base, and livecart_app still
    # has no use for it: its privileges are not given back.
    pass
