"""Keep the reason of the model's answer with the receipt of the LLM fallback.

The model answers a closed reason with its reading of an ambiguous comment
(``LlmReason``): ``order``, or why the comment orders nothing. The receipt
stored its proposed product, variant and quantity, but not that reason, so
an ``ok`` receipt that proposed nothing could not say why. ``llm_reason``
holds it, one of the closed list, and is set exactly when the outcome is
``ok``.

Written by hand with the list spelled out, as 0002 is: a revision builds the
schema of its day, whatever the enum becomes later.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LLM_REASONS = (
    "order",
    "no_order",
    "unknown_product",
    "unclear_variant",
    "unclear_quantity",
    "several_products",
)


def upgrade() -> None:
    op.add_column("comments", sa.Column("llm_reason", sa.String(32), nullable=True))
    op.create_check_constraint(
        "ck_comments_llm_reason",
        "comments",
        sa.column("llm_reason", sa.String).in_(LLM_REASONS),
    )
    op.create_check_constraint(
        "ck_comments_llm_reason_when_ok",
        "comments",
        (sa.column("llm_outcome", sa.String) == "ok")
        == sa.column("llm_reason", sa.String).is_not(None),
    )


def downgrade() -> None:
    op.drop_constraint("ck_comments_llm_reason_when_ok", "comments", type_="check")
    op.drop_constraint("ck_comments_llm_reason", "comments", type_="check")
    op.drop_column("comments", "llm_reason")
