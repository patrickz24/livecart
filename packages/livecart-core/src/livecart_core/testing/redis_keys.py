"""Per-test Redis key prefixes, so that tests sharing database 15 never see each other's keys.

Every Redis key of the application will sit under a configurable prefix. A test
passes the prefix of the ``redis_key_prefix`` fixture, and the fixture deletes
every key under it when the test ends, whatever its outcome.
"""

import re
from uuid import uuid4

from redis.asyncio import Redis

TEST_KEY_NAMESPACE = "test"

# Characters with a meaning in the glob patterns of SCAN MATCH.
_GLOB_SPECIAL = re.compile(r"([*?\[\]\\])")


def new_test_key_prefix() -> str:
    """A prefix no other test uses: ``test:<32 hexadecimal digits>``."""
    return f"{TEST_KEY_NAMESPACE}:{uuid4().hex}"


async def delete_keys_with_prefix(client: Redis, prefix: str) -> int:
    """Delete every key named ``<prefix>:...`` and return how many were deleted."""
    literal = _GLOB_SPECIAL.sub(r"\\\1", prefix)
    keys = [key async for key in client.scan_iter(match=f"{literal}:*", count=500)]
    if not keys:
        return 0
    return int(await client.unlink(*keys))
