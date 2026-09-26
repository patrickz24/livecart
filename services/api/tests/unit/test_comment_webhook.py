"""The comment webhook over the fakes: signature first, then the limit, the batch and the queue."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import cast

import httpx2
import pytest

from livecart_api.app import MAX_REQUEST_BODY_BYTES, create_app
from livecart_api.rate_limits import TOO_MANY_REQUESTS
from livecart_api.routes.webhooks import (
    INVALID_SIGNATURE,
    QUEUE_FULL,
    QUEUE_FULL_RETRY_AFTER_S,
)
from livecart_api.testing import (
    SIMULATOR_WEBHOOK,
    current_time_clock,
    make_settings,
    memory_resources,
    open_client,
    signed_webhook,
    simulator_only,
)
from livecart_core.application.dto import CommentBatch, Platform, QueuedComment
from livecart_core.application.ingest_comments import BACKLOG_TTL, IngestComments
from livecart_core.application.ports import (
    LiveSessionRepository,
    OrderLookup,
    ProductStockRepository,
    RefreshTokenRepository,
    SellerRepository,
)
from livecart_core.settings import AppEnv
from livecart_core.signing import SIGNATURE_HEADER
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeCommentQueue,
    FakePlatformAdapter,
    InMemoryDatabase,
    InMemoryRateLimiter,
)

BATCH = CommentBatch(
    comments=[
        incoming_comment("je prends A12 M", external_comment_id="c-1"),
        incoming_comment("SOLD B07 x2", external_comment_id="c-2", author="buyer-2"),
    ]
)
REFUSED = {"detail": INVALID_SIGNATURE}


@dataclass
class Api:
    client: httpx2.AsyncClient
    clock: FakeClock
    queue: FakeCommentQueue
    limiter: InMemoryRateLimiter
    ingest: IngestComments

    async def queued(self) -> list[QueuedComment]:
        deliveries = await self.queue.receive("worker-1", max_messages=1000, block_seconds=0)
        return [delivery.comment for delivery in deliveries]

    async def post(
        self, batch: CommentBatch | bytes = BATCH, *, age_seconds: int = 0
    ) -> httpx2.Response:
        headers, body = signed_webhook(batch, self.clock, age_seconds=age_seconds)
        return await self.client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)


@asynccontextmanager
async def _open_api(**settings: object) -> AsyncIterator[Api]:
    """The app over the fakes, with ``settings`` replacing some of the tests' settings."""
    clock = current_time_clock()
    queue, limiter = FakeCommentQueue(clock), InMemoryRateLimiter(clock)
    resources = memory_resources(
        InMemoryDatabase(), clock=clock, rate_limiter=limiter, comment_queue=queue
    )
    app = create_app(make_settings(AppEnv.DEVELOPMENT, **settings), resources)
    async with open_client(app) as client:
        yield Api(client, clock, queue, limiter, app.state.ingest_comments)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with _open_api() as opened:
        yield opened


# --- Accepted --------------------------------------------------------------------------


async def test_a_signed_batch_is_queued_and_acknowledged_with_202(api: Api) -> None:
    response = await api.post()

    assert response.status_code == 202
    assert response.json() == {"accepted": 2}
    queued = await api.queued()
    assert [envelope.comment for envelope in queued] == list(BATCH.comments)
    # One request id for the batch, the one the response carries; one correlation id each.
    assert {envelope.request_id for envelope in queued} == {response.headers["X-Request-ID"]}
    correlation_ids = [uuid.UUID(envelope.correlation_id) for envelope in queued]
    assert len(set(correlation_ids)) == 2
    assert {envelope.enqueued_at for envelope in queued} == {api.clock.now()}
    assert api.ingest.comments_received_total == 2


async def test_a_batch_of_200_comments_is_accepted(api: Api) -> None:
    batch = CommentBatch(
        comments=[incoming_comment(external_comment_id=f"c-{n}") for n in range(200)]
    )

    response = await api.post(batch)

    assert (response.status_code, response.json()) == (202, {"accepted": 200})
    assert len(await api.queued()) == 200


# --- The signature, before anything else -----------------------------------------------


async def test_an_unsigned_forged_or_stale_batch_gets_one_generic_401(api: Api) -> None:
    headers, body = signed_webhook(BATCH, api.clock)
    signature = headers[SIGNATURE_HEADER]
    # Another last digit, whatever it was.
    forged = {**headers, SIGNATURE_HEADER: signature[:-1] + ("1" if signature[-1] == "0" else "0")}
    stale_headers, _ = signed_webhook(BATCH, api.clock, age_seconds=301)

    answers = [
        await api.client.post(SIMULATOR_WEBHOOK, content=body),
        await api.client.post(SIMULATOR_WEBHOOK, content=body, headers=forged),
        await api.client.post(SIMULATOR_WEBHOOK, content=body, headers=stale_headers),
        await api.client.post(SIMULATOR_WEBHOOK, content=body + b" ", headers=headers),
        await api.client.post(
            SIMULATOR_WEBHOOK,
            content=body,
            headers=[(SIGNATURE_HEADER, headers[SIGNATURE_HEADER])] * 2,
        ),
    ]

    assert [answer.status_code for answer in answers] == [401] * 5
    assert all(answer.json() == REFUSED for answer in answers)
    assert await api.queued() == []
    # A refused call fills no counter: only signed calls count.
    assert api.limiter.windows == {}


async def test_the_signature_is_checked_before_the_body_is_read(api: Api) -> None:
    response = await api.client.post(SIMULATOR_WEBHOOK, content=b"{not json")

    assert (response.status_code, response.json()) == (401, REFUSED)


async def test_a_signature_up_to_300_s_old_is_accepted(api: Api) -> None:
    response = await api.post(age_seconds=300)

    assert response.status_code == 202


async def test_a_platform_not_enabled_answers_404_and_an_unknown_one_422(api: Api) -> None:
    headers, body = signed_webhook(BATCH, api.clock)

    disabled = await api.client.post(
        "/api/v1/webhooks/comments/facebook", content=body, headers=headers
    )
    unknown = await api.client.post(
        "/api/v1/webhooks/comments/myspace", content=body, headers=headers
    )

    assert (disabled.status_code, disabled.json()) == (404, {"detail": "Not found"})
    assert unknown.status_code == 422
    assert await api.queued() == []


async def test_a_body_over_1_mib_is_refused_before_its_signature_is_checked(api: Api) -> None:
    response = await api.post(b"x" * (MAX_REQUEST_BODY_BYTES + 1))

    assert response.status_code == 413
    assert await api.queued() == []


# --- The rate limit, once signed -------------------------------------------------------


async def test_signed_calls_are_limited_per_platform() -> None:
    clock = current_time_clock()
    queue, limiter = FakeCommentQueue(clock), InMemoryRateLimiter(clock)
    tiktok = FakePlatformAdapter(Platform.TIKTOK)
    resources = memory_resources(
        InMemoryDatabase(),
        clock=clock,
        rate_limiter=limiter,
        comment_queue=queue,
        platforms={**simulator_only(clock), Platform.TIKTOK: tiktok},
    )
    app = create_app(make_settings(rate_limit_comment_webhook_per_minute=2), resources)
    headers, body = signed_webhook(BATCH, clock)
    tiktok_batch = CommentBatch(comments=[incoming_comment(platform=Platform.TIKTOK)])
    tiktok_headers, tiktok_body = tiktok.signed(tiktok_batch)

    async with open_client(app) as client:
        unsigned = [
            (await client.post(SIMULATOR_WEBHOOK, content=body)).status_code for _ in range(3)
        ]
        signed = [
            (await client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)).status_code
            for _ in range(2)
        ]
        refused = await client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)
        other = await client.post(
            "/api/v1/webhooks/comments/tiktok", content=tiktok_body, headers=tiktok_headers
        )

    assert unsigned == [401, 401, 401]
    assert signed == [202, 202]
    assert refused.status_code == 429
    assert refused.json() == {"detail": TOO_MANY_REQUESTS}
    assert refused.headers["Retry-After"] == "60"
    assert other.status_code == 202
    assert len(await queue.receive("worker-1", max_messages=100, block_seconds=0)) == 5


# --- The batch -------------------------------------------------------------------------


def _comment(**changes: object) -> dict[str, object]:
    return {**json.loads(incoming_comment().model_dump_json()), **changes}


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        (b"{not json", "json_invalid"),
        (b'{"comments": []}', "too_short"),
        (json.dumps({"comments": [_comment()] * 201}).encode(), "too_long"),
        (json.dumps({"comments": [_comment()], "extra": 1}).encode(), "extra_forbidden"),
        (json.dumps({"comments": [_comment(text="secret-" * 200)]}).encode(), "string_too_long"),
        (
            json.dumps({"comments": [_comment(posted_at="2026-09-26T12:00:00")]}).encode(),
            "timezone_aware",
        ),
    ],
    ids=["not json", "empty", "201 comments", "extra field", "long text", "naive time"],
)
async def test_a_signed_body_that_is_no_batch_answers_422_without_echoing_it(
    api: Api, body: bytes, error_type: str
) -> None:
    response = await api.post(body)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert error_type in {error["type"] for error in errors}
    assert all(error["loc"][0] == "body" for error in errors)
    assert all("input" not in error for error in errors)
    assert "secret-secret" not in response.text
    assert await api.queued() == []


async def test_a_comment_of_another_platform_answers_422(api: Api) -> None:
    batch = CommentBatch(
        comments=[
            incoming_comment(external_comment_id="c-1"),
            incoming_comment(external_comment_id="c-2", platform=Platform.FACEBOOK),
        ]
    )

    response = await api.post(batch)

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "loc": ["body", "comments", 1, "platform"],
            "msg": "Must be the platform of the webhook: simulator",
            "type": "platform_mismatch",
        }
    ]
    assert await api.queued() == []


# --- The queue and its backpressure ----------------------------------------------------


async def test_a_full_queue_answers_503_until_the_workers_catch_up() -> None:
    async with _open_api(queue_max_depth=3) as api:
        accepted = [(await api.post()).status_code for _ in range(2)]
        full = await api.post()
        # The workers consume and acknowledge everything.
        for delivery in await api.queue.receive("worker-1", max_messages=10, block_seconds=0):
            await api.queue.ack(delivery)
        still_cached = await api.post()
        api.clock.advance(BACKLOG_TTL)
        again = await api.post()

    # The second batch still went in: 2 < 3 before it, 4 after.
    assert accepted == [202, 202]
    assert full.status_code == 503
    assert full.json() == {"detail": QUEUE_FULL}
    assert full.headers["Retry-After"] == str(QUEUE_FULL_RETRY_AFTER_S)
    assert still_cached.status_code == 503
    assert again.status_code == 202
    assert api.ingest.queue_backpressure_total == 2
    assert await api.queue.backlog() == 2


async def test_a_comment_too_large_once_queued_answers_422() -> None:
    async with _open_api(queue_max_envelope_bytes=1_000) as api:
        batch = CommentBatch(
            comments=[
                incoming_comment(external_comment_id="c-1"),
                incoming_comment("😀" * 1000, external_comment_id="c-2"),
            ]
        )
        response = await api.post(batch)
        queued = await api.queued()

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "loc": ["body", "comments", 1],
            "msg": "The comment takes over 1000 bytes once queued",
            "type": "envelope_too_large",
        }
    ]
    assert queued == []


async def test_a_queue_that_did_not_take_the_batch_answers_503(api: Api) -> None:
    api.queue.fail_next()

    response = await api.post()

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert response.headers["Retry-After"] == "1"
    assert api.ingest.comments_received_total == 0


class _NoDatabase:
    """A repository that fails on any use: the webhook must never reach PostgreSQL."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the comment webhook used the database ({name})")


async def test_the_webhook_never_touches_the_database() -> None:
    clock = current_time_clock()
    queue = FakeCommentQueue(clock)
    no_database = _NoDatabase()
    resources = replace(
        memory_resources(InMemoryDatabase(), clock=clock, comment_queue=queue),
        sellers=cast(SellerRepository, no_database),
        refresh_tokens=cast(RefreshTokenRepository, no_database),
        live_sessions=cast(LiveSessionRepository, no_database),
        products=cast(ProductStockRepository, no_database),
        orders=cast(OrderLookup, no_database),
    )
    headers, body = signed_webhook(BATCH, clock)

    async with open_client(create_app(make_settings(), resources)) as client:
        response = await client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)

    assert response.status_code == 202
    assert await queue.backlog() == 2


def test_the_openapi_document_describes_the_webhook() -> None:
    clock = current_time_clock()
    app = create_app(make_settings(), memory_resources(InMemoryDatabase(), clock=clock))

    operation = app.openapi()["paths"]["/api/v1/webhooks/comments/{platform}"]["post"]

    assert operation["operationId"] == "receive_comment_webhook"
    assert set(operation["responses"]) == {"202", "401", "404", "413", "422", "429", "503"}
    assert "X-LiveCart-Signature" in operation["requestBody"]["description"]
    assert "security" not in operation
