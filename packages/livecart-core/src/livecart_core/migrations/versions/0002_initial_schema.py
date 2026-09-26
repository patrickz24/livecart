"""Initial schema: every table of the data model, with its constraints and indexes.

Written by hand, with its closed lists spelled out rather than imported from
the code: a revision builds the schema of its day, whatever the enums become
later. ``tests/integration/test_schema.py`` builds the metadata of
``livecart_core.infrastructure.db.tables`` in a scratch schema and compares
the catalog of both, so this revision and the metadata cannot drift apart.

The tables are created by livecart_migrator, so the default privileges of
infra/postgres/grants.sql give livecart_app SELECT, INSERT and UPDATE on each
of them, and nothing else: no DELETE, no TRUNCATE.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORMS = ("simulator", "facebook", "instagram", "tiktok")
PAYMENT_PROVIDERS = ("mock", "stripe", "paypal", "viva")
REJECTION_REASONS = (
    "no_intent",
    "multiple_products",
    "quantity_out_of_bounds",
    "variant_not_found",
    "ambiguous_unresolved",
    "session_not_found",
    "session_not_live",
)
PARSE_REASONS = (
    "no_intent",
    "multiple_products",
    "quantity_out_of_bounds",
    "variant_not_found",
    "unknown_code",
    "product_by_name",
    "unresolved_variant",
    "conflicting_signals",
)
LLM_OUTCOMES = (
    "ok",
    "disabled",
    "budget_exceeded",
    "llm_busy",
    "timeout",
    "error",
    "refused",
    "truncated",
    "invalid",
)

# Dropped in reverse order of their foreign keys.
TABLES = (
    "sellers",
    "refresh_tokens",
    "live_sessions",
    "products",
    "comments",
    "purchase_intents",
    "orders",
    "payments",
    "processed_webhook_events",
    "shipments",
)


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid, nullable=False)


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=f"pk_{table}")


def _fk(table: str, column: str, referred: str, *, ondelete: str | None = None) -> sa.Constraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{referred}.id"], name=f"fk_{table}_{column}_{referred}", ondelete=ondelete
    )


def _timestamp(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at() -> sa.Column[Any]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _identifier(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.String(128), nullable=nullable)


def _one_of(column: str, values: Sequence[str], *, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sa.column(column, sa.String).in_(list(values)), name=name)


def _at_least(column: str, minimum: int, *, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sa.column(column, sa.BigInteger) >= minimum, name=name)


def _quantity(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        sa.column("quantity", sa.Integer).between(1, 99), name=f"ck_{table}_quantity"
    )


def _currency(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        sa.column("currency", sa.CHAR).regexp_match("^[A-Z]{3}$"), name=f"ck_{table}_currency"
    )


def _sha256(column: str, *, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        sa.func.octet_length(sa.column(column, sa.LargeBinary)) == 32, name=name
    )


def upgrade() -> None:
    op.create_table(
        "sellers",
        _id(),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        _created_at(),
        _updated_at(),
        _pk("sellers"),
        sa.UniqueConstraint("slug", name="uq_sellers_slug"),
        sa.CheckConstraint(
            sa.column("slug", sa.String).regexp_match("^[a-z0-9]+(-[a-z0-9]+)*$"),
            name="ck_sellers_slug",
        ),
    )
    op.create_index("uq_sellers_email", "sellers", [sa.func.lower(sa.column("email"))], unique=True)

    op.create_table(
        "refresh_tokens",
        _id(),
        sa.Column("seller_id", sa.Uuid, nullable=False),
        sa.Column("family_id", sa.Uuid, nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False),
        _timestamp("expires_at", nullable=False),
        _timestamp("used_at"),
        _timestamp("revoked_at"),
        sa.Column("replaced_by_id", sa.Uuid, nullable=True),
        _created_at(),
        _updated_at(),
        _pk("refresh_tokens"),
        _fk("refresh_tokens", "seller_id", "sellers"),
        _fk("refresh_tokens", "replaced_by_id", "refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        _sha256("token_hash", name="ck_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "live_sessions",
        _id(),
        sa.Column("seller_id", sa.Uuid, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        _identifier("external_live_id"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        _timestamp("started_at"),
        _timestamp("ended_at"),
        _created_at(),
        _updated_at(),
        _pk("live_sessions"),
        _fk("live_sessions", "seller_id", "sellers"),
        sa.UniqueConstraint(
            "platform", "external_live_id", name="uq_live_sessions_platform_external_live_id"
        ),
        _one_of("platform", PLATFORMS, name="ck_live_sessions_platform"),
        _one_of("status", ("draft", "live", "ended"), name="ck_live_sessions_status"),
    )
    op.create_index("ix_live_sessions_seller_id", "live_sessions", ["seller_id"])

    op.create_table(
        "products",
        _id(),
        sa.Column("session_id", sa.Uuid, nullable=False),
        sa.Column("code", sa.String(7), nullable=False),
        sa.Column("variant", sa.String(32), nullable=False, server_default=""),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("price_minor", sa.BigInteger, nullable=False),
        sa.Column("stock_total", sa.Integer, nullable=False),
        sa.Column("reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sold", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "available",
            sa.Integer,
            sa.Computed(
                sa.column("stock_total", sa.Integer)
                - sa.column("reserved", sa.Integer)
                - sa.column("sold", sa.Integer),
                persisted=True,
            ),
            nullable=False,
        ),
        _created_at(),
        _updated_at(),
        _pk("products"),
        _fk("products", "session_id", "live_sessions"),
        sa.UniqueConstraint(
            "session_id", "code", "variant", name="uq_products_session_id_code_variant"
        ),
        sa.CheckConstraint(
            sa.column("code", sa.String).regexp_match("^[A-Z]{1,3}[0-9]{1,4}$"),
            name="ck_products_code",
        ),
        sa.CheckConstraint(
            sa.and_(
                sa.column("reserved", sa.Integer) >= 0,
                sa.column("sold", sa.Integer) >= 0,
                sa.column("reserved", sa.Integer) + sa.column("sold", sa.Integer)
                <= sa.column("stock_total", sa.Integer),
            ),
            name="ck_products_stock",
        ),
        _at_least("price_minor", 0, name="ck_products_price_minor"),
    )

    op.create_table(
        "comments",
        _id(),
        sa.Column("platform", sa.String(16), nullable=False),
        _identifier("external_comment_id"),
        sa.Column("session_id", sa.Uuid, nullable=True),
        _identifier("author_platform_user_id"),
        sa.Column("text", sa.String(1000), nullable=False),
        _identifier("correlation_id"),
        sa.Column("status", sa.String(16), nullable=False, server_default="received"),
        _identifier("claimed_by", nullable=True),
        _timestamp("claimed_until"),
        sa.Column("rejection_reason", sa.String(32), nullable=True),
        sa.Column("parse_result", sa.String(16), nullable=True),
        sa.Column("parse_reason", sa.String(32), nullable=True),
        sa.Column("llm_outcome", sa.String(16), nullable=True),
        sa.Column("llm_product_code", sa.String(32), nullable=True),
        sa.Column("llm_variant", sa.String(64), nullable=True),
        sa.Column("llm_quantity", sa.Integer, nullable=True),
        sa.Column("llm_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("llm_request_id", sa.String(128), nullable=True),
        sa.Column("llm_model", sa.String(128), nullable=True),
        sa.Column("llm_input_tokens", sa.Integer, nullable=True),
        sa.Column("llm_output_tokens", sa.Integer, nullable=True),
        sa.Column("llm_latency_ms", sa.Integer, nullable=True),
        _created_at(),
        _updated_at(),
        _pk("comments"),
        _fk("comments", "session_id", "live_sessions", ondelete="SET NULL"),
        sa.UniqueConstraint(
            "platform", "external_comment_id", name="uq_comments_platform_external_comment_id"
        ),
        _one_of("platform", PLATFORMS, name="ck_comments_platform"),
        _one_of(
            "status",
            ("received", "processed", "rejected", "dead_lettered"),
            name="ck_comments_status",
        ),
        _one_of("rejection_reason", REJECTION_REASONS, name="ck_comments_rejection_reason"),
        sa.CheckConstraint(
            (sa.column("status", sa.String) == "rejected")
            == sa.column("rejection_reason", sa.String).is_not(None),
            name="ck_comments_rejection_reason_when_rejected",
        ),
        _one_of(
            "parse_result", ("match", "ambiguous", "rejected"), name="ck_comments_parse_result"
        ),
        _one_of("parse_reason", PARSE_REASONS, name="ck_comments_parse_reason"),
        _one_of("llm_outcome", LLM_OUTCOMES, name="ck_comments_llm_outcome"),
    )
    op.create_index(
        "ix_comments_rejected_session_id_rejection_reason",
        "comments",
        ["session_id", "rejection_reason"],
        postgresql_where=sa.column("status", sa.String) == "rejected",
    )

    op.create_table(
        "purchase_intents",
        _id(),
        sa.Column("comment_id", sa.Uuid, nullable=False),
        sa.Column("session_id", sa.Uuid, nullable=False),
        sa.Column("product_id", sa.Uuid, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False),
        sa.Column("evidence", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        _created_at(),
        _pk("purchase_intents"),
        _fk("purchase_intents", "comment_id", "comments"),
        _fk("purchase_intents", "session_id", "live_sessions"),
        _fk("purchase_intents", "product_id", "products"),
        sa.UniqueConstraint("comment_id", name="uq_purchase_intents_comment_id"),
        _quantity("purchase_intents"),
        _one_of("source", ("parser", "llm"), name="ck_purchase_intents_source"),
        sa.CheckConstraint(
            sa.column("confidence", sa.Numeric).between(0, 1),
            name="ck_purchase_intents_confidence",
        ),
        _one_of(
            "outcome",
            ("reserved", "out_of_stock", "already_reserved"),
            name="ck_purchase_intents_outcome",
        ),
    )
    op.create_index("ix_purchase_intents_session_id", "purchase_intents", ["session_id"])

    op.create_table(
        "orders",
        _id(),
        sa.Column("reference", sa.CHAR(9), nullable=False),
        sa.Column("access_token_hash", sa.LargeBinary, nullable=False),
        sa.Column("seller_id", sa.Uuid, nullable=False),
        sa.Column("session_id", sa.Uuid, nullable=False),
        sa.Column("intent_id", sa.Uuid, nullable=False),
        sa.Column("product_id", sa.Uuid, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger, nullable=False),
        sa.Column("total_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending_payment"),
        _timestamp("reserved_until", nullable=False),
        _timestamp("expiry_lease_until"),
        _identifier("buyer_platform_user_id"),
        _timestamp("buyer_notified_at"),
        sa.Column("shipping_address", postgresql.JSONB, nullable=True),
        _timestamp("paid_at"),
        _timestamp("expired_at"),
        _created_at(),
        _updated_at(),
        _pk("orders"),
        _fk("orders", "seller_id", "sellers"),
        _fk("orders", "session_id", "live_sessions"),
        _fk("orders", "intent_id", "purchase_intents"),
        _fk("orders", "product_id", "products"),
        sa.UniqueConstraint("reference", name="uq_orders_reference"),
        sa.UniqueConstraint("intent_id", name="uq_orders_intent_id"),
        sa.CheckConstraint(
            sa.column("reference", sa.CHAR).regexp_match(
                "^LC-[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{6}$"
            ),
            name="ck_orders_reference",
        ),
        _sha256("access_token_hash", name="ck_orders_access_token_hash"),
        _quantity("orders"),
        _at_least("unit_price_minor", 0, name="ck_orders_unit_price_minor"),
        sa.CheckConstraint(
            sa.column("total_minor", sa.BigInteger)
            == sa.column("unit_price_minor", sa.BigInteger) * sa.column("quantity", sa.Integer),
            name="ck_orders_total_minor",
        ),
        _currency("orders"),
        _one_of(
            "status",
            ("pending_payment", "paid", "expired", "paid_after_expiry"),
            name="ck_orders_status",
        ),
    )
    op.create_index(
        "uq_orders_pending_buyer_product",
        "orders",
        ["session_id", "product_id", "buyer_platform_user_id"],
        unique=True,
        postgresql_where=sa.column("status", sa.String) == "pending_payment",
    )
    op.create_index(
        "ix_orders_pending_reserved_until",
        "orders",
        ["reserved_until"],
        postgresql_where=sa.column("status", sa.String) == "pending_payment",
    )
    op.create_index("ix_orders_session_id", "orders", ["session_id"])

    op.create_table(
        "payments",
        _id(),
        sa.Column("order_id", sa.Uuid, nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_ref", sa.String(255), nullable=True),
        sa.Column("redirect_url", sa.String(2048), nullable=True),
        sa.Column("return_url", sa.String(2048), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("provider_payment_ref", sa.String(255), nullable=True),
        _created_at(),
        _updated_at(),
        _pk("payments"),
        _fk("payments", "order_id", "orders"),
        sa.UniqueConstraint("order_id", "attempt_no", name="uq_payments_order_id_attempt_no"),
        sa.UniqueConstraint("provider_ref", name="uq_payments_provider_ref"),
        _at_least("attempt_no", 1, name="ck_payments_attempt_no"),
        _one_of("provider", PAYMENT_PROVIDERS, name="ck_payments_provider"),
        _at_least("amount_minor", 0, name="ck_payments_amount_minor"),
        _currency("payments"),
        _one_of("status", ("pending", "succeeded", "failed", "expired"), name="ck_payments_status"),
    )
    op.create_index(
        "uq_payments_order_id_pending",
        "payments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.column("status", sa.String) == "pending",
    )

    op.create_table(
        "processed_webhook_events",
        _id(),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("delivery_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _timestamp("processed_at"),
        sa.Column("outcome", sa.String(16), nullable=True),
        _created_at(),
        _updated_at(),
        _pk("processed_webhook_events"),
        sa.UniqueConstraint(
            "provider", "event_id", name="uq_processed_webhook_events_provider_event_id"
        ),
        _one_of("provider", PAYMENT_PROVIDERS, name="ck_processed_webhook_events_provider"),
        _at_least("delivery_count", 1, name="ck_processed_webhook_events_delivery_count"),
        _at_least("attempts", 0, name="ck_processed_webhook_events_attempts"),
        _one_of(
            "outcome", ("applied", "ignored", "failed"), name="ck_processed_webhook_events_outcome"
        ),
        sa.CheckConstraint(
            sa.column("processed_at", sa.DateTime).is_(None)
            == sa.column("outcome", sa.String).is_(None),
            name="ck_processed_webhook_events_outcome_when_processed",
        ),
    )
    op.create_index(
        "ix_processed_webhook_events_unprocessed_created_at",
        "processed_webhook_events",
        ["created_at"],
        postgresql_where=sa.column("processed_at", sa.DateTime).is_(None),
    )

    op.create_table(
        "shipments",
        _id(),
        sa.Column("order_id", sa.Uuid, nullable=False),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("carrier_ref", sa.String(128), nullable=False),
        sa.Column("tracking_number", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="created"),
        _timestamp("carrier_created_at", nullable=False),
        _timestamp("last_event_at"),
        _created_at(),
        _updated_at(),
        _pk("shipments"),
        _fk("shipments", "order_id", "orders"),
        sa.UniqueConstraint("order_id", name="uq_shipments_order_id"),
        sa.UniqueConstraint("tracking_number", name="uq_shipments_tracking_number"),
        _one_of("carrier", ("mock", "dpd", "chronopost", "dhl"), name="ck_shipments_carrier"),
        _one_of(
            "status",
            ("created", "in_transit", "out_for_delivery", "delivered", "cancelled"),
            name="ck_shipments_status",
        ),
    )


def downgrade() -> None:
    # Indexes and constraints go with their tables.
    for table in reversed(TABLES):
        op.drop_table(table)
