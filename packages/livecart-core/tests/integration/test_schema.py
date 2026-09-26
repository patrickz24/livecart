"""The migrated schema of livecart_test, against the metadata of the adapters and the plan's rules.

The first test builds ``livecart_core.infrastructure.db.tables.metadata`` in a
scratch schema and compares PostgreSQL's own description of both schemas:
every column (type, nullability, default, generation), every constraint and
every index, as ``format_type``, ``pg_get_constraintdef`` and
``pg_get_indexdef`` print them. The hand-written revisions and the metadata
therefore cannot drift apart. The other tests write rows as livecart_app to
check the rules the schema itself enforces.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.tables import metadata

pytestmark = pytest.mark.integration

PROBE_SCHEMA = "metadata_probe"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
GENERATED_ALWAYS = "428C9"
# No password verifies against it, as with the "!" of a locked account in /etc/shadow.
LOCKED_ACCOUNT = "!"

_COLUMNS = text(
    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
    " a.attgenerated::text, pg_get_expr(d.adbin, d.adrelid)"
    " FROM pg_attribute a"
    " JOIN pg_class c ON c.oid = a.attrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE n.nspname = :schema AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped"
    " AND c.relname <> 'alembic_version'"
)
_CONSTRAINTS = text(
    "SELECT c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid)"
    " FROM pg_constraint con"
    " JOIN pg_class c ON c.oid = con.conrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = :schema AND c.relname <> 'alembic_version'"
)
_INDEXES = text(
    "SELECT tablename, indexname, indexdef FROM pg_indexes"
    " WHERE schemaname = :schema AND tablename <> 'alembic_version'"
)


@dataclass(frozen=True)
class Catalog:
    columns: Mapping[tuple[str, str], tuple[Any, ...]]
    constraints: Mapping[tuple[str, str], tuple[Any, ...]]
    indexes: Mapping[tuple[str, str], str]


async def _catalog(connection: AsyncConnection, schema: str) -> Catalog:
    # Names that the search path resolves are printed unqualified: set it to the
    # schema under study, so that both schemas print alike.
    await connection.execute(select(func.set_config("search_path", schema, True)))
    parameters = {"schema": schema}
    columns = (await connection.execute(_COLUMNS, parameters)).all()
    constraints = (await connection.execute(_CONSTRAINTS, parameters)).all()
    indexes = (await connection.execute(_INDEXES, parameters)).all()
    return Catalog(
        columns={(row[0], row[1]): tuple(row[2:]) for row in columns},
        constraints={(row[0], row[1]): tuple(row[2:]) for row in constraints},
        indexes={(row[0], row[1]): row[2].replace(f" {schema}.", " ") for row in indexes},
    )


@pytest.fixture
async def catalogs(
    database_schema: None, migrator_engine: AsyncEngine
) -> AsyncIterator[tuple[Catalog, Catalog]]:
    """(what the migrations built, what the metadata describes)."""
    async with migrator_engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA IF EXISTS {PROBE_SCHEMA} CASCADE"))
        await connection.execute(text(f"CREATE SCHEMA {PROBE_SCHEMA}"))
    try:
        async with migrator_engine.begin() as connection:
            probe = await connection.execution_options(schema_translate_map={None: PROBE_SCHEMA})
            await probe.run_sync(metadata.create_all)
        async with migrator_engine.begin() as connection:
            migrated = await _catalog(connection, "public")
            described = await _catalog(connection, PROBE_SCHEMA)
        yield migrated, described
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {PROBE_SCHEMA} CASCADE"))


async def test_the_migrations_build_the_tables_the_metadata_describes(
    catalogs: tuple[Catalog, Catalog],
) -> None:
    migrated, described = catalogs

    assert {table for table, _ in migrated.columns} == set(metadata.tables)
    assert migrated.columns == described.columns


async def test_the_migrations_build_the_constraints_the_metadata_describes(
    catalogs: tuple[Catalog, Catalog],
) -> None:
    migrated, described = catalogs

    assert migrated.constraints == described.constraints


async def test_the_migrations_build_the_indexes_the_metadata_describes(
    catalogs: tuple[Catalog, Catalog],
) -> None:
    migrated, described = catalogs

    assert migrated.indexes == described.indexes


async def test_available_is_a_stored_generated_column(catalogs: tuple[Catalog, Catalog]) -> None:
    migrated, _ = catalogs

    type_name, not_null, generated, expression = migrated.columns[("products", "available")]
    assert (type_name, not_null, generated) == ("integer", True, "s")
    assert expression == "((stock_total - reserved) - sold)"


# --- Rules the schema enforces, written as livecart_app ---------------------------------


def _sqlstate_and_constraint(error: DBAPIError) -> tuple[str | None, str | None]:
    cause = error.orig.__cause__ if error.orig is not None else None
    return getattr(error.orig, "sqlstate", None), getattr(cause, "constraint_name", None)


async def _seller(connection: AsyncConnection, email: str | None = None) -> UUID:
    seller_id = uuid4()
    await connection.execute(
        insert(tables.sellers).values(
            id=seller_id,
            email=email or f"{seller_id.hex}@example.test",
            slug=f"seller-{seller_id.hex}",
            display_name="Seller",
            password_hash=LOCKED_ACCOUNT,
        )
    )
    return seller_id


async def _product(connection: AsyncConnection, *, stock_total: int = 10) -> UUID:
    session_id, product_id = uuid4(), uuid4()
    await connection.execute(
        insert(tables.live_sessions).values(
            id=session_id,
            seller_id=await _seller(connection),
            title="Friday drop",
            platform="simulator",
            external_live_id=f"live-{session_id.hex}",
        )
    )
    await connection.execute(
        insert(tables.products).values(
            id=product_id,
            session_id=session_id,
            code="A12",
            name="Robe fleurie",
            price_minor=2_500,
            stock_total=stock_total,
        )
    )
    return product_id


async def test_available_follows_the_counters(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    async with app_engine.begin() as connection:
        product_id = await _product(connection, stock_total=10)
        await connection.execute(
            update(tables.products)
            .where(tables.products.c.id == product_id)
            .values(reserved=3, sold=2)
        )
        row = (
            await connection.execute(
                select(tables.products.c.variant, tables.products.c.available).where(
                    tables.products.c.id == product_id
                )
            )
        ).one()

    assert tuple(row) == ("", 5)


async def test_available_cannot_be_written(clean_database: None, app_engine: AsyncEngine) -> None:
    async with app_engine.begin() as connection:
        product_id = await _product(connection)

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                update(tables.products)
                .where(tables.products.c.id == product_id)
                .values(available=100)
            )

    assert _sqlstate_and_constraint(excinfo.value)[0] == GENERATED_ALWAYS


@pytest.mark.parametrize(
    ("reserved", "sold"),
    [(11, 0), (0, 11), (6, 5), (-1, 0), (0, -1)],
)
async def test_the_counters_never_oversell(
    clean_database: None, app_engine: AsyncEngine, reserved: int, sold: int
) -> None:
    async with app_engine.begin() as connection:
        product_id = await _product(connection, stock_total=10)

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                update(tables.products)
                .where(tables.products.c.id == product_id)
                .values(reserved=reserved, sold=sold)
            )

    assert _sqlstate_and_constraint(excinfo.value) == (CHECK_VIOLATION, "ck_products_stock")


@pytest.mark.parametrize("code", ["a12", "A", "12", "ABCD1", "A12345", "A-12"])
async def test_a_product_code_has_the_catalog_shape(
    clean_database: None, app_engine: AsyncEngine, code: str
) -> None:
    async with app_engine.begin() as connection:
        product_id = await _product(connection)

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                update(tables.products).where(tables.products.c.id == product_id).values(code=code)
            )

    assert _sqlstate_and_constraint(excinfo.value) == (CHECK_VIOLATION, "ck_products_code")


async def test_a_seller_email_is_unique_whatever_its_case(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    async with app_engine.begin() as connection:
        await _seller(connection, email="Camille@Example.test")

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await _seller(connection, email="camille@example.TEST")

    assert _sqlstate_and_constraint(excinfo.value) == (UNIQUE_VIOLATION, "uq_sellers_email")


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("status", "paused", "ck_live_sessions_status"),
        ("platform", "youtube", "ck_live_sessions_platform"),
    ],
)
async def test_closed_lists_refuse_unknown_values(
    clean_database: None, app_engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    async with app_engine.begin() as connection:
        session_id = uuid4()
        await connection.execute(
            insert(tables.live_sessions).values(
                id=session_id,
                seller_id=await _seller(connection),
                title="Friday drop",
                platform="simulator",
                external_live_id="live-1",
            )
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with app_engine.begin() as connection:
            await connection.execute(
                update(tables.live_sessions)
                .where(tables.live_sessions.c.id == session_id)
                .values({column: value})
            )

    assert _sqlstate_and_constraint(excinfo.value) == (CHECK_VIOLATION, constraint)


async def test_a_rejected_comment_carries_its_reason_and_only_it_does(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    comment_id = uuid4()
    async with app_engine.begin() as connection:
        await connection.execute(
            insert(tables.comments).values(
                id=comment_id,
                platform="simulator",
                external_comment_id="comment-1",
                author_platform_user_id="buyer-1",
                text="je prends A12",
                correlation_id="correlation-1",
                created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
            )
        )

    for values in ({"status": "rejected"}, {"rejection_reason": "no_intent"}):
        with pytest.raises(DBAPIError) as excinfo:
            async with app_engine.begin() as connection:
                await connection.execute(
                    update(tables.comments).where(tables.comments.c.id == comment_id).values(values)
                )
        assert _sqlstate_and_constraint(excinfo.value) == (
            CHECK_VIOLATION,
            "ck_comments_rejection_reason_when_rejected",
        )


async def test_an_ok_receipt_carries_the_models_reason_and_only_it_does(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    comment_id = uuid4()
    async with app_engine.begin() as connection:
        await connection.execute(
            insert(tables.comments).values(
                id=comment_id,
                platform="simulator",
                external_comment_id="comment-1",
                author_platform_user_id="buyer-1",
                text="je prends deux B07",
                correlation_id="correlation-1",
                created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
            )
        )

    for values, constraint in (
        ({"llm_outcome": "ok"}, "ck_comments_llm_reason_when_ok"),
        ({"llm_outcome": "llm_busy", "llm_reason": "order"}, "ck_comments_llm_reason_when_ok"),
        ({"llm_outcome": "ok", "llm_reason": "maybe"}, "ck_comments_llm_reason"),
    ):
        with pytest.raises(DBAPIError) as excinfo:
            async with app_engine.begin() as connection:
                await connection.execute(
                    update(tables.comments).where(tables.comments.c.id == comment_id).values(values)
                )
        assert _sqlstate_and_constraint(excinfo.value) == (CHECK_VIOLATION, constraint)
