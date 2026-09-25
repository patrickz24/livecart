from pydantic import SecretStr
from sqlalchemy.pool import AsyncAdaptedQueuePool

from livecart_core.infrastructure.db.engine import create_db_engine
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
