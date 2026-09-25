"""Redis isolation of the integration tests: database 15 and one key prefix per test."""

import pytest
from redis.asyncio import Redis

from livecart_core.testing.redis_keys import delete_keys_with_prefix, new_test_key_prefix

pytestmark = pytest.mark.integration


async def test_the_client_uses_database_15(redis_client: Redis) -> None:
    info = await redis_client.client_info()

    assert int(info["db"]) == 15


async def test_each_test_starts_with_no_key_under_its_prefix(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    assert redis_key_prefix.startswith("test:")
    assert [key async for key in redis_client.scan_iter(match=f"{redis_key_prefix}:*")] == []


async def test_only_the_keys_under_the_prefix_are_deleted(redis_client: Redis) -> None:
    mine, other = new_test_key_prefix(), new_test_key_prefix()
    await redis_client.set(f"{mine}:a", "1")
    await redis_client.set(f"{mine}:b:c", "2")
    await redis_client.set(f"{other}:a", "3")
    # A key that only starts like the prefix, without its separator.
    await redis_client.set(f"{mine}x", "4")
    try:
        assert await delete_keys_with_prefix(redis_client, mine) == 2

        assert await redis_client.exists(f"{mine}:a", f"{mine}:b:c") == 0
        assert await redis_client.exists(f"{other}:a", f"{mine}x") == 2
    finally:
        await redis_client.delete(f"{other}:a", f"{mine}x")


async def test_glob_characters_in_a_prefix_match_themselves(redis_client: Redis) -> None:
    base = new_test_key_prefix()
    await redis_client.set(f"{base}*:a", "1")
    await redis_client.set(f"{base}b:a", "2")
    try:
        assert await delete_keys_with_prefix(redis_client, f"{base}*") == 1

        assert await redis_client.exists(f"{base}b:a") == 1
    finally:
        await redis_client.delete(f"{base}*:a", f"{base}b:a")
