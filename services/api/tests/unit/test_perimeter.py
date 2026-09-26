"""The HTTP perimeter of the app: security headers, trusted hosts, no CORS, a 1 MiB body limit."""

from collections.abc import AsyncIterator

import httpx2
import pytest

from livecart_api.app import MAX_REQUEST_BODY_BYTES
from livecart_api.middleware.security_headers import API_CSP, HSTS
from livecart_api.testing import TEST_ORIGIN, make_app, make_settings, open_client
from livecart_core.settings import AppEnv

LOGIN = "/api/v1/auth/login"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": API_CSP,
}


def _security_headers(response: httpx2.Response) -> dict[str, str]:
    return {name: response.headers[name] for name in SECURITY_HEADERS if name in response.headers}


# --- Security headers ------------------------------------------------------------------


async def test_every_answer_carries_the_security_headers() -> None:
    app = make_app(settings=make_settings(AppEnv.DEVELOPMENT, rate_limit_login_per_minute=1))

    async with open_client(app) as client:
        answers = {
            "ok": await client.get("/health"),
            "not found": await client.get("/api/v1/nothing-here"),
            "unauthorized": await client.get("/api/v1/live-sessions"),
            "invalid": await client.post(LOGIN, json={}),
            "too many": await client.post(LOGIN, json={}),
            "bad host": await client.get("/health", headers={"Host": "evil.example"}),
            "too large": await client.post(LOGIN, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1)),
        }

    assert {name: answer.status_code for name, answer in answers.items()} == {
        "ok": 200,
        "not found": 404,
        "unauthorized": 401,
        "invalid": 422,
        "too many": 429,
        "bad host": 400,
        "too large": 413,
    }
    for answer in answers.values():
        assert _security_headers(answer) == SECURITY_HEADERS
        assert "strict-transport-security" not in answer.headers


async def test_hsts_is_sent_once_enabled() -> None:
    app = make_app(settings=make_settings(AppEnv.PRODUCTION, hsts_enabled=True))

    async with open_client(app) as client:
        response = await client.get("/health")

    assert (
        response.headers["strict-transport-security"]
        == HSTS
        == ("max-age=31536000; includeSubDomains")
    )


async def test_the_development_docs_page_keeps_every_header_but_the_csp() -> None:
    async with open_client(make_app(app_env=AppEnv.DEVELOPMENT)) as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")

    # Swagger UI runs an inline script and loads its bundle from a CDN.
    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-frame-options"] == "DENY"
    assert _security_headers(schema) == SECURITY_HEADERS


async def test_production_serves_no_page_without_the_csp() -> None:
    async with open_client(make_app(app_env=AppEnv.PRODUCTION)) as client:
        docs = await client.get("/docs")

    assert docs.status_code == 404
    assert docs.headers["content-security-policy"] == API_CSP


# --- CORS ------------------------------------------------------------------------------


async def test_no_answer_lets_another_origin_read_it() -> None:
    cross_origin = {"Origin": "https://evil.example"}

    async with open_client(make_app()) as client:
        read = await client.get("/health", headers=cross_origin)
        preflight = await client.options(
            LOGIN, headers={**cross_origin, "Access-Control-Request-Method": "POST"}
        )
        same_origin = await client.get("/health", headers={"Origin": TEST_ORIGIN})

    # Everything is served on one origin through nginx: there is no CORS to configure.
    assert read.status_code == 200
    assert preflight.status_code == 405
    for response in (read, preflight, same_origin):
        assert not any(name.startswith("access-control-") for name in response.headers)


# --- Trusted hosts ---------------------------------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "localhost:8080", "127.0.0.1:8000", "api:8000"])
async def test_the_allowed_hosts_are_answered_whatever_the_port(host: str) -> None:
    settings = make_settings(allowed_hosts=("localhost", "127.0.0.1", "api"))

    async with open_client(make_app(settings=settings)) as client:
        response = await client.get("/health", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "", "[::1]"])
async def test_any_other_host_is_refused_before_any_route(host: str) -> None:
    settings = make_settings(allowed_hosts=("localhost", "127.0.0.1", "api"))

    async with open_client(make_app(settings=settings)) as client:
        response = await client.get("/api/v1/auth/me", headers={"Host": host})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


# --- Body limit ------------------------------------------------------------------------


async def _chunks(total: int, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        chunk = min(size, total - sent)
        sent += chunk
        yield b"x" * chunk


async def test_a_body_over_1_mib_is_refused_whether_announced_or_streamed() -> None:
    async with open_client(make_app()) as client:
        announced = await client.post(LOGIN, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
        streamed = await client.post(LOGIN, content=_chunks(MAX_REQUEST_BODY_BYTES + 1))
        at_the_limit = await client.post(LOGIN, content=b"x" * MAX_REQUEST_BODY_BYTES)

    assert MAX_REQUEST_BODY_BYTES == 1024 * 1024
    assert "content-length" in announced.request.headers
    assert "content-length" not in streamed.request.headers
    assert (announced.status_code, streamed.status_code) == (413, 413)
    # Starlette answers an announced length in plain text, before any route
    # runs, and a stream it cuts short through the route's JSON error handler.
    assert announced.text == "Content Too Large"
    assert streamed.json() == {"detail": "Content Too Large"}
    # 1 MiB itself passes the limit, and is then refused as no login at all.
    assert at_the_limit.status_code == 422


async def test_a_large_body_to_a_route_that_reads_none_is_refused_too() -> None:
    async with open_client(make_app()) as client:
        response = await client.request(
            "GET", "/health", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1)
        )

    assert response.status_code == 413
