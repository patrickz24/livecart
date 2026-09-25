"""Bind the request id to every log line written while a request is handled."""

import structlog
from asgi_correlation_id import correlation_id
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestContextMiddleware:
    """Copy the id chosen by ``CorrelationIdMiddleware`` into the structlog context.

    It must run inside ``CorrelationIdMiddleware``, which validates an incoming
    ``X-Request-ID`` (a UUID, so it cannot inject text into logs) or generates
    one, and echoes it in the response. uvicorn's access log line is written
    while the response starts, inside this context, so it carries the id too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with structlog.contextvars.bound_contextvars(request_id=correlation_id.get()):
            await self.app(scope, receive, send)
