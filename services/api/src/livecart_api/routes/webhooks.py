"""The comment webhook of the live platforms: ``POST /api/v1/webhooks/comments/{platform}``.

A platform posts its live comments here, and the API acknowledges them as
soon as they are queued: the workers do the rest. In this order, each step
before the next one reads anything more:

1. the body is capped at 1 MiB, by nginx and by the API's middleware (413);
2. ``PlatformAdapter.verify`` checks the signature on the raw body, before
   anything is parsed, counted or queued: a missing or wrong signature is a
   401 whose body never says which check failed. A platform that is not in
   ``ENABLED_PLATFORMS`` has no adapter and answers 404;
3. the platform's rate limit, 6000 calls a minute, counts only signed calls
   (429 beyond);
4. ``PlatformAdapter.parse`` reads the platform's format into the common one,
   checked against ``CommentBatch``: 1 to 200 comments, each within its
   bounds, and each of the path's platform (422 otherwise);
5. ``IngestComments`` puts each comment in its envelope onto the queue: 202
   with the number queued, or 503 when the queue did not take them all (the
   platform retries, and the inbox deduplicates).

Nothing on this path touches PostgreSQL.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import uuid4

import structlog
from asgi_correlation_id import correlation_id
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError

from livecart_api.errors import NOT_FOUND, documented
from livecart_api.rate_limits import TOO_MANY_REQUESTS, RateLimiting
from livecart_core.application.dto import MAX_BATCH_SIZE, CommentBatch, IncomingComment, Platform
from livecart_core.application.ingest_comments import IngestComments
from livecart_core.application.ports import InvalidSignature, PlatformAdapter

logger = structlog.get_logger(__name__)

PREFIX: Final = "/api/v1/webhooks/comments"
INVALID_SIGNATURE: Final = "Invalid signature"
# The body is each platform's own format: only the simulator's is the common one.
REQUEST_BODY: Final = {
    "required": True,
    "description": (
        "The platform's webhook payload, signed with its own header. The simulator's is "
        "a CommentBatch, signed with X-LiveCart-Signature: t=<unix time>,v1=<hex "
        "HMAC-SHA256 of '<t>.<body>' under COMMENT_WEBHOOK_SECRET_SIMULATOR>."
    ),
    "content": {"application/json": {"schema": {"type": "object"}}},
}


class WebhookAccepted(BaseModel):
    """A batch acknowledged: every comment of it is queued for the workers."""

    accepted: int = Field(ge=1, le=MAX_BATCH_SIZE, description="The comments queued.")


def _invalid_body(errors: Sequence[Mapping[str, Any]]) -> RequestValidationError:
    """A 422 in FastAPI's shape, located in the body, without the refused values."""
    return RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


def _read_comments(adapter: PlatformAdapter, raw_body: bytes) -> list[IncomingComment]:
    """The comments of a verified body, as a valid batch of the adapter's platform."""
    try:
        comments = adapter.parse(raw_body)
        batch = CommentBatch(comments=comments)
    except ValidationError as error:
        raise _invalid_body(
            error.errors(include_url=False, include_context=False, include_input=False)
        ) from None
    except ValueError:
        # Another adapter's own refusal: its message may quote the body.
        refusal = {"loc": (), "msg": "The body is no comment batch", "type": "value_error"}
        raise _invalid_body([refusal]) from None
    mismatched = [
        {
            "loc": ("comments", index, "platform"),
            "msg": f"Must be the platform of the webhook: {adapter.platform}",
            "type": "platform_mismatch",
        }
        for index, comment in enumerate(batch.comments)
        if comment.platform is not adapter.platform
    ]
    if mismatched:
        raise _invalid_body(mismatched)
    return list(batch.comments)


def build_webhook_router(
    adapters: Mapping[Platform, PlatformAdapter], ingest: IngestComments, limits: RateLimiting
) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["webhooks"])

    @router.post(
        "/{platform}",
        status_code=status.HTTP_202_ACCEPTED,
        summary="Receive a batch of live comments from a platform",
        openapi_extra={"requestBody": REQUEST_BODY},
        responses={
            **documented(
                {
                    status.HTTP_401_UNAUTHORIZED: "The signature is missing, wrong or too old",
                    status.HTTP_404_NOT_FOUND: "The platform's webhook is not enabled",
                    status.HTTP_429_TOO_MANY_REQUESTS: TOO_MANY_REQUESTS,
                    status.HTTP_503_SERVICE_UNAVAILABLE: (
                        "The comment queue did not take the batch: try again after Retry-After"
                    ),
                }
            ),
            # Answered by the middleware, in plain text when the length is announced.
            status.HTTP_413_CONTENT_TOO_LARGE: {"description": "The body is over 1 MiB"},
        },
    )
    async def receive_comment_webhook(platform: Platform, request: Request) -> WebhookAccepted:
        adapter = adapters.get(platform)
        if adapter is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        raw_body = await request.body()
        try:
            adapter.verify(request.headers, raw_body)
        except InvalidSignature:
            logger.warning("comment_webhook_signature_refused", platform=platform.value)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=INVALID_SIGNATURE) from None
        await limits.enforce(limits.rules.comment_webhook, platform.value)
        comments = _read_comments(adapter, raw_body)
        # Set by CorrelationIdMiddleware on every request; a fresh id otherwise.
        request_id = correlation_id.get() or uuid4().hex
        accepted = await ingest(comments, request_id=request_id)
        logger.info("comment_webhook_accepted", platform=platform.value, comments=accepted)
        return WebhookAccepted(accepted=accepted)

    return router
