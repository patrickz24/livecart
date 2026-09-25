"""The probes used by /ready and the worker heartbeat, against the real servers."""

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.infrastructure.db.engine import ping_database
from livecart_core.infrastructure.redis_client import ping_redis

pytestmark = pytest.mark.integration


async def test_select_one_succeeds_as_the_runtime_role(app_engine: AsyncEngine) -> None:
    await ping_database(app_engine)


async def test_ping_succeeds_with_the_redis_password(redis_client: Redis) -> None:
    await ping_redis(redis_client)
