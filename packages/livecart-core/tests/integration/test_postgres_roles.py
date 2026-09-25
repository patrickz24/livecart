"""Roles and grants created by infra/postgres, checked on the compose livecart_test database."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"


async def _sqlstate_of(engine: AsyncEngine, statement: str) -> str | None:
    with pytest.raises(DBAPIError) as excinfo:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    return getattr(excinfo.value.orig, "sqlstate", None)


async def test_each_role_connects_to_the_test_database(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    for engine, role in ((app_engine, "livecart_app"), (migrator_engine, "livecart_migrator")):
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT current_user, current_database()"))).one()
        assert tuple(row) == (role, "livecart_test")


async def test_neither_role_has_elevated_attributes(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
                    " rolbypassrls FROM pg_roles"
                    " WHERE rolname IN ('livecart_app', 'livecart_migrator') ORDER BY rolname"
                )
            )
        ).all()

    assert [tuple(row) for row in rows] == [
        ("livecart_app", False, False, False, False, False),
        ("livecart_migrator", False, False, False, False, False),
    ]


async def test_the_migrator_owns_the_database(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as connection:
        owner = (
            await connection.execute(
                text(
                    "SELECT pg_get_userbyid(datdba) FROM pg_database"
                    " WHERE datname = current_database()"
                )
            )
        ).scalar_one()

    assert owner == "livecart_migrator"


async def test_the_runtime_role_reads_and_writes_but_never_deletes(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    async with migrator_engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS grant_probe"))
        await connection.execute(text("CREATE TABLE grant_probe (id int PRIMARY KEY, note text)"))
    try:
        # Default privileges: a table created by the migrator is readable and writable.
        async with app_engine.begin() as connection:
            await connection.execute(text("INSERT INTO grant_probe VALUES (1, 'created')"))
            await connection.execute(text("UPDATE grant_probe SET note = 'updated'"))
            note = (await connection.execute(text("SELECT note FROM grant_probe"))).scalar_one()
        assert note == "updated"

        for forbidden in (
            "DELETE FROM grant_probe",
            "TRUNCATE grant_probe",
            "ALTER TABLE grant_probe ADD COLUMN extra int",
            "DROP TABLE grant_probe",
        ):
            assert await _sqlstate_of(app_engine, forbidden) == INSUFFICIENT_PRIVILEGE, forbidden
    finally:
        async with migrator_engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS grant_probe"))


async def test_the_runtime_role_cannot_create_objects(app_engine: AsyncEngine) -> None:
    for forbidden in (
        "CREATE TABLE app_owned (id int)",
        "CREATE TEMPORARY TABLE app_scratch (id int)",
    ):
        assert await _sqlstate_of(app_engine, forbidden) == INSUFFICIENT_PRIVILEGE, forbidden
