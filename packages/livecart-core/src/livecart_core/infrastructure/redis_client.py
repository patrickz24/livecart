"""Redis client factory: a client, and its connection pool, per use of a process."""

from typing import Final

from pydantic import SecretStr
from redis.asyncio import Redis

# redis-py's own default, written out: the pool of a process's shared client.
# A command that finds every connection in use fails at once with
# MaxConnectionsError, so a use that holds connections for long, such as the
# API's event streams, gets a client of its own (livecart_api.resources).
SHARED_POOL_CONNECTIONS: Final = 100


def create_redis_client(url: SecretStr, *, max_connections: int = SHARED_POOL_CONNECTIONS) -> Redis:
    """Create a client of at most ``max_connections``; none is opened until first use."""
    if max_connections < 1:
        raise ValueError(f"a pool holds one connection at least, not {max_connections}")
    return Redis.from_url(url.get_secret_value(), max_connections=max_connections)


async def ping_redis(client: Redis) -> None:
    """Send ``PING``; raise if Redis cannot be reached or rejects the credentials."""
    await client.ping()
