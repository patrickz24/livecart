"""The client address behind nginx: uvicorn trusts ``X-Forwarded-For`` from nginx's address alone.

The app is served through uvicorn's own ASGI stack (``uvicorn.Config.load``,
what ``uvicorn`` builds when it starts), configured from the api command of
compose.yaml as uvicorn's command line parses it. nginx overwrites
``X-Forwarded-For`` with its peer's address (infra/nginx/default.conf), so
the header it sends holds one address; the rate limits then count
``request.client`` (``livecart_api.rate_limits``).
"""

import re
from pathlib import Path
from typing import Final

import httpx2
import pytest
import uvicorn
from starlette.types import ASGIApp

from livecart_api.testing import make_app, make_settings, open_client
from livecart_core.application.dto import RateLimitKey
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import FakeClock, InMemoryRateLimiter

COMPOSE: Final = Path(__file__).resolve().parents[4] / "compose.yaml"
LOOKUP: Final = "/api/v1/orders/LC-ZZZZZZ"


def _service_block(compose: str, service: str) -> str:
    """The lines of one service of compose.yaml, up to the next service."""
    match = re.search(rf"^  {service}:\n((?:    .*\n|\n)*)", compose, flags=re.MULTILINE)
    assert match is not None, f"no {service} service in compose.yaml"
    return match.group(1)


def _api_command() -> list[str]:
    """The api service's command, with ${NGINX_IP:-default} replaced by its default."""
    compose = COMPOSE.read_text(encoding="utf-8")
    block = _service_block(compose, "api")
    items = re.search(r"^    command:\n((?:      - .*\n)+)", block, flags=re.MULTILINE)
    assert items is not None, "the api service has no command list"
    command = [line.strip().removeprefix("- ").strip('"') for line in items.group(1).splitlines()]
    return [re.sub(r"\$\{NGINX_IP:-([^}]+)\}", r"\1", item) for item in command]


def _nginx_address() -> str:
    block = _service_block(COMPOSE.read_text(encoding="utf-8"), "web")
    address = re.search(r"ipv4_address: \$\{NGINX_IP:-([0-9.]+)\}", block)
    assert address is not None, "the web service has no fixed address"
    return address.group(1)


NGINX: Final = _nginx_address()


def _served_like_compose(app: ASGIApp) -> ASGIApp:
    """``app`` wrapped as uvicorn wraps it with the api command of compose.yaml."""
    program, *arguments = _api_command()
    assert program == "uvicorn"
    options = uvicorn.main.make_context("uvicorn", arguments).params
    config = uvicorn.Config(
        app,
        proxy_headers=options["proxy_headers"],
        forwarded_allow_ips=options["forwarded_allow_ips"],
        log_config=None,
        lifespan="off",
    )
    config.load()
    served: ASGIApp = config.loaded_app
    return served


def _stack(**limits: int) -> tuple[ASGIApp, InMemoryRateLimiter]:
    limiter = InMemoryRateLimiter(FakeClock())
    app = make_app(rate_limiter=limiter, settings=make_settings(AppEnv.DEVELOPMENT, **limits))
    return _served_like_compose(app), limiter


async def _lookup(stack: ASGIApp, *, peer: str, forwarded_for: str | None) -> httpx2.Response:
    headers = {} if forwarded_for is None else {"X-Forwarded-For": forwarded_for}
    async with open_client(stack, peer=peer) as client:
        return await client.get(LOOKUP, headers=headers)


def test_compose_trusts_the_forwarded_headers_of_nginx_only() -> None:
    command = _api_command()

    assert "--proxy-headers" in command
    trusted = command[command.index("--forwarded-allow-ips") + 1]
    # Never *: any client could then choose the address its limits count.
    assert trusted == NGINX == "172.30.0.10"


async def test_two_clients_behind_nginx_get_counters_of_their_own() -> None:
    stack, limiter = _stack(rate_limit_buyer_lookup_per_minute=1)

    first = await _lookup(stack, peer=NGINX, forwarded_for="203.0.113.7")
    again = await _lookup(stack, peer=NGINX, forwarded_for="203.0.113.7")
    other = await _lookup(stack, peer=NGINX, forwarded_for="198.51.100.1")

    assert (first.status_code, again.status_code, other.status_code) == (404, 429, 404)
    assert {key.subject for key in limiter.windows if key.rule == "buyer_lookup"} == {
        "203.0.113.7",
        "198.51.100.1",
    }


async def test_a_forwarded_address_from_another_peer_is_ignored() -> None:
    stack, limiter = _stack(rate_limit_buyer_lookup_per_minute=1)

    # A client that reaches uvicorn without nginx cannot pick its address.
    forged = await _lookup(stack, peer="10.1.2.3", forwarded_for="203.0.113.7")
    forged_again = await _lookup(stack, peer="10.1.2.3", forwarded_for="198.51.100.1")

    assert (forged.status_code, forged_again.status_code) == (404, 429)
    assert RateLimitKey("buyer_lookup", "10.1.2.3") in limiter.windows
    assert not any(key.subject in {"203.0.113.7", "198.51.100.1"} for key in limiter.windows)


@pytest.mark.parametrize("forwarded_for", [None, "10.9.9.9"])
async def test_nginx_itself_is_one_client_when_it_forwards_nothing_else(
    forwarded_for: str | None,
) -> None:
    stack, limiter = _stack()

    # A request without the header counts as nginx's; one naming another peer counts as that one.
    await _lookup(stack, peer=NGINX, forwarded_for=forwarded_for)

    expected = NGINX if forwarded_for is None else forwarded_for
    assert RateLimitKey("buyer_lookup", expected) in limiter.windows


async def test_two_ipv6_addresses_of_one_slash_64_share_a_counter() -> None:
    stack, limiter = _stack(rate_limit_buyer_lookup_per_minute=1)

    first = await _lookup(stack, peer=NGINX, forwarded_for="2001:db8:1:2::1")
    same_block = await _lookup(stack, peer=NGINX, forwarded_for="2001:db8:1:2:ffff:ffff:ffff:9")
    next_block = await _lookup(stack, peer=NGINX, forwarded_for="2001:db8:1:3::1")

    assert (first.status_code, same_block.status_code, next_block.status_code) == (404, 429, 404)
    assert RateLimitKey("buyer_lookup", "2001:db8:1:2::/64") in limiter.windows
