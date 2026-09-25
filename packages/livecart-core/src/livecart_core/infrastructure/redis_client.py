"""Redis client factory: one client, and its connection pool, per process."""

from pydantic import SecretStr
from redis.asyncio import Redis


def create_redis_client(url: SecretStr) -> Redis:
    """Create the process-wide client; no connection is opened until first use."""
    return Redis.from_url(url.get_secret_value())


async def ping_redis(client: Redis) -> None:
    """Send ``PING``; raise if Redis cannot be reached or rejects the credentials."""
    await client.ping()
