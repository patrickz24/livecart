"""``RedisCommentQueue`` against a scripted client: what a real Redis cannot be made to do on cue.

A pipeline that fails halfway (an out-of-memory refusal under ``noeviction``
after some entries went in), a connection lost during a batch, and the exact
commands a batch and a receive send.
"""

from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from livecart_core.application.ports import ServiceUnavailable
from livecart_core.infrastructure.comment_queue import RedisCommentQueue, parse_stream_id
from livecart_core.testing.builders import queued_comment

OOM = ResponseError("OOM command not allowed when used memory > 'maxmemory'.")


class _Pipeline:
    def __init__(self, outcome: list[object] | Exception) -> None:
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.executions = 0
        self._outcome = outcome

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("xadd", args, kwargs))

    async def execute(self, *, raise_on_error: bool = True) -> list[object]:
        self.executions += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _Client:
    """The few commands of redis-py that these tests reach, answering as scripted."""

    def __init__(self, outcome: list[object] | Exception | None = None) -> None:
        self.pipelines: list[tuple[bool, _Pipeline]] = []
        self.reads: list[dict[str, Any]] = []
        self._outcome: list[object] | Exception = outcome if outcome is not None else []

    def register_script(self, script: str) -> object:
        return object()

    def pipeline(self, *, transaction: bool = True) -> _Pipeline:
        pipeline = _Pipeline(self._outcome)
        self.pipelines.append((transaction, pipeline))
        return pipeline

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> list[object]:
        self.reads.append(kwargs)
        return []


def _queue(client: _Client) -> RedisCommentQueue:
    return RedisCommentQueue(cast(Redis, client), prefix="unit")


async def test_a_batch_is_one_pipeline_of_uncapped_appends() -> None:
    client = _Client([b"1-0", b"1-1", b"1-2"])
    comments = [queued_comment(external_comment_id=f"c-{n}") for n in range(3)]

    await _queue(client).enqueue(comments)

    [(transaction, pipeline)] = client.pipelines
    assert transaction is False
    assert pipeline.executions == 1
    assert [args for _, args, _ in pipeline.commands] == [
        ("unit:queue:comments", {"envelope": comment.model_dump_json()}) for comment in comments
    ]
    # No MAXLEN, MINID or LIMIT: a cap would drop comments nobody has read.
    assert all(kwargs == {} for _, _, kwargs in pipeline.commands)


async def test_a_pipeline_that_fails_halfway_is_unavailable() -> None:
    client = _Client([b"1-0", OOM, OOM])
    comments = [queued_comment(external_comment_id=f"c-{n}") for n in range(3)]

    with pytest.raises(ServiceUnavailable, match="2 of 3 comments not queued") as excinfo:
        await _queue(client).enqueue(comments)

    assert "maxmemory" not in str(excinfo.value)


async def test_a_connection_lost_during_a_batch_is_unavailable() -> None:
    client = _Client(RedisConnectionError("Connection reset by peer"))

    with pytest.raises(ServiceUnavailable, match=r"enqueue.*ConnectionError"):
        await _queue(client).enqueue([queued_comment()])


async def test_an_empty_batch_sends_nothing() -> None:
    client = _Client()

    await _queue(client).enqueue([])

    assert client.pipelines == []


@pytest.mark.parametrize(
    ("block_seconds", "block"), [(0, None), (0.0001, 1), (0.05, 50), (2, 2000)]
)
async def test_a_receive_blocks_for_the_whole_milliseconds_asked_and_never_forever(
    block_seconds: float, block: int | None
) -> None:
    client = _Client()

    queue = _queue(client)
    received = await queue.receive("worker-1", max_messages=32, block_seconds=block_seconds)

    assert received == []

    # BLOCK 0 would wait forever: no wait sends no BLOCK at all.
    assert client.reads == [{"count": 32, "block": block}]


def test_stream_ids_sort_as_redis_sorts_them() -> None:
    assert parse_stream_id("1695000000000-12") == (1_695_000_000_000, 12)
    assert parse_stream_id(b"9-0") < parse_stream_id(b"10-0")
    assert parse_stream_id("10-2") < parse_stream_id("10-10")
