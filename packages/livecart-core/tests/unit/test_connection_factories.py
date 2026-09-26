from pydantic import SecretStr
from sqlalchemy.pool import AsyncAdaptedQueuePool

from livecart_core.infrastructure.db.engine import API_POOL, WORKER_POOL, create_db_engine
from livecart_core.infrastructure.redis_client import create_redis_client


async def test_engine_is_pooled_and_hides_bound_parameters() -> None:
    engine = create_db_engine(
        SecretStr("postgresql+asyncpg://livecart_app:s3cr3t@db.invalid:5432/livecart"),
        application_name="livecart-test",
        pool_size=5,
        max_overflow=10,
    )
    try:
        pool = engine.pool
        assert isinstance(pool, AsyncAdaptedQueuePool)
        assert pool.size() == 5
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.sync_engine.hide_parameters
        assert "s3cr3t" not in repr(engine)
    finally:
        await engine.dispose()


async def test_redis_client_uses_the_url_credentials_and_database() -> None:
    client = create_redis_client(SecretStr("redis://:s3cr3t@cache.invalid:6380/15"))
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache.invalid", 6380, 15)
        assert kwargs["password"] == "s3cr3t"
    finally:
        await client.aclose()


# PostgreSQL's 100 connections, less its 3 reserved for superusers.
AVAILABLE_CONNECTIONS = 97


def test_the_pools_fit_the_connection_budget_with_three_workers() -> None:
    assert (API_POOL.max_connections, WORKER_POOL.max_connections) == (15, 22)
    assert API_POOL.max_connections + WORKER_POOL.max_connections == 37
    assert API_POOL.max_connections + 3 * WORKER_POOL.max_connections == 81
    assert API_POOL.max_connections + 3 * WORKER_POOL.max_connections <= AVAILABLE_CONNECTIONS
