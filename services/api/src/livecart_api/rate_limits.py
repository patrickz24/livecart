"""Rate limits of the HTTP routes: fixed windows in Redis, answered with 429 and ``Retry-After``.

Each limit is a ``RateLimitRule`` counted by the ``RateLimiter`` port
(``{p}:rl:{rule}:{subject}`` in Redis), with the plan's defaults, which the
settings may change:

- ``POST /api/v1/auth/login``: 10 requests a minute per client address, and 5
  failed logins per 15 minutes per (account, address). The failures are
  counted per address too, so that nobody can lock a seller out of the live
  by failing on purpose from elsewhere;
- every seller route (``CurrentSeller``): 300 requests a minute per seller;
- ``GET /api/v1/orders/{reference}``, open to anyone: 30 requests and 20
  not-found answers a minute per address. Every not-found answer is also
  counted for all addresses together, and a warning is logged when they pass
  ``BUYER_LOOKUP_NOT_FOUND_ALERT_PER_MINUTE``: references guessed from many
  addresses at once;
- the buyer's token routes (for now ``GET /api/v1/orders/{reference}/details``):
  60 requests a minute per order access token, counted by its SHA-256, or per
  address when no token is sent. A whole live audience behind one mobile
  carrier's address (CGNAT) holds as many tokens as orders. Any value counts
  as a token there, so their not-found answers, a wrong token's included,
  count against the address with the lookup's 20: a client that sends a new
  value with every request is stopped all the same, while the verified tokens
  of one address never fill that counter;
- ``GET /api/v1/storefronts/{seller_slug}``, open to anyone: 120 requests a
  minute per address, a limit the plan does not name. Every viewer opens it,
  so it is the most generous, and it still caps the three queries of each
  request;
- ``POST /api/v1/webhooks/comments/{platform}``: 6000 calls a minute per
  platform, counted once the signature holds, so that a forger fills no
  platform's counter; a full queue answers 503 instead, which is
  backpressure, not abuse. The calls whose signature fails count per address
  instead, 60 a minute: past them the address gets 429 before its body is
  read, since each refusal costs the API a body of up to 1 MiB to read and
  hash, and a log line.

The client address is ``request.client``, which uvicorn rewrites from
``X-Forwarded-For`` only when the connection comes from nginx
(``--forwarded-allow-ips``), and nginx overwrites that header with its own
peer: a client cannot choose its address. IPv6 addresses count by /64, the
block one subscriber usually gets, so that rotating through it gains nothing.

When the counters cannot be read (Redis down), the login is refused with 503:
its limits are all that stops password guessing. Everything else goes on
without a limit, and the failure is counted and logged. The refresh route has
no limit at all: its token of 256 bits cannot be guessed, so a Redis outage
never logs a seller out.
"""

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Final, Self

import structlog
from fastapi import Request, status
from fastapi.responses import JSONResponse

from livecart_core.application.dto import RateLimitKey, RateLimitRule, WindowCount
from livecart_core.application.ports import RateLimiter, ServiceUnavailable
from livecart_core.settings import Settings

logger = structlog.get_logger(__name__)

MINUTE: Final = 60
# The block of IPv6 addresses one subscriber usually receives.
IPV6_GROUP_PREFIX: Final = 64
# What an address that is none counts as: never the case behind uvicorn.
UNKNOWN_CLIENT: Final = "unknown"
# The subject of the counter of all addresses together.
EVERYONE: Final = "all"
TOO_MANY_REQUESTS: Final = "Too many requests: try again after Retry-After seconds"


class TooManyRequests(Exception):
    """A request over its rule's limit: 429, with the seconds until its window ends."""

    def __init__(self, rule: str, retry_after_seconds: int) -> None:
        super().__init__(f"rate limit {rule} reached")
        self.rule = rule
        # A refused request always has a running window: at least one second to wait.
        self.retry_after_seconds = max(1, retry_after_seconds)


def group_address(host: str | None) -> str:
    """The subject a client address counts as: IPv4 as it is, IPv6 by its /64."""
    if host is None:
        return UNKNOWN_CLIENT
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return UNKNOWN_CLIENT
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    if address.ipv4_mapped is not None:
        # ::ffff:203.0.113.7 is the IPv4 client of a dual-stack socket.
        return str(address.ipv4_mapped)
    return str(ipaddress.IPv6Network((int(address), IPV6_GROUP_PREFIX), strict=False))


def client_address(request: Request) -> str:
    """The grouped address of the request's client, as uvicorn resolved it."""
    return group_address(request.client.host if request.client is not None else None)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def login_failure_subject(email: str, address: str) -> str:
    """(account, address): the account by the SHA-256 of its e-mail, which stays out of Redis."""
    # lower() as in the uq_sellers_email index: any case names the same account.
    return f"{_digest(email.lower())[:32]}:{address}"


def token_subject(token: str) -> str:
    """An access token by its SHA-256: the token itself never reaches Redis."""
    return _digest(token)


@dataclass(frozen=True, slots=True)
class RateLimitRules:
    """The rule of each limited route; see the module docstring."""

    login: RateLimitRule
    login_failures: RateLimitRule
    seller: RateLimitRule
    buyer_lookup: RateLimitRule
    buyer_lookup_not_found: RateLimitRule
    buyer_lookup_not_found_alert: RateLimitRule
    buyer_token: RateLimitRule
    storefront: RateLimitRule
    comment_webhook: RateLimitRule
    comment_webhook_signature_failures: RateLimitRule

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            login=RateLimitRule(
                "login", settings.rate_limit_login_per_minute, MINUTE, fail_closed=True
            ),
            login_failures=RateLimitRule(
                "login_failures",
                settings.rate_limit_login_failures,
                settings.rate_limit_login_failure_window_seconds,
                fail_closed=True,
            ),
            seller=RateLimitRule("seller", settings.rate_limit_seller_per_minute, MINUTE),
            buyer_lookup=RateLimitRule(
                "buyer_lookup", settings.rate_limit_buyer_lookup_per_minute, MINUTE
            ),
            buyer_lookup_not_found=RateLimitRule(
                "buyer_lookup_not_found",
                settings.rate_limit_buyer_lookup_not_found_per_minute,
                MINUTE,
            ),
            buyer_lookup_not_found_alert=RateLimitRule(
                "buyer_lookup_not_found_all",
                settings.buyer_lookup_not_found_alert_per_minute,
                MINUTE,
            ),
            buyer_token=RateLimitRule(
                "buyer_token", settings.rate_limit_buyer_token_per_minute, MINUTE
            ),
            storefront=RateLimitRule(
                "storefront", settings.rate_limit_storefront_per_minute, MINUTE
            ),
            comment_webhook=RateLimitRule(
                "comment_webhook", settings.rate_limit_comment_webhook_per_minute, MINUTE
            ),
            comment_webhook_signature_failures=RateLimitRule(
                "comment_webhook_signature_failures",
                settings.rate_limit_comment_webhook_signature_failures_per_minute,
                MINUTE,
            ),
        )


class RateLimiting:
    """Applies the rules through the ``RateLimiter`` port, and counts what the metrics show.

    The two counters are plain integers, which the API's metrics read at
    scrape time as ``livecart_rate_limiter_errors_total`` and
    ``livecart_buyer_lookup_not_found_total`` (``livecart_api.metrics``).
    """

    def __init__(self, limiter: RateLimiter, rules: RateLimitRules) -> None:
        self.rules = rules
        self._limiter = limiter
        self.limiter_errors_total = 0
        self.buyer_lookup_not_found_total = 0

    async def enforce(self, rule: RateLimitRule, subject: str) -> None:
        """Count this request, and raise ``TooManyRequests`` when it goes over the limit."""
        window = await self._count(rule, subject, hit=True)
        if window is not None and window.hits > rule.limit:
            raise TooManyRequests(rule.name, window.resets_in_seconds)

    async def check(self, rule: RateLimitRule, subject: str) -> None:
        """Raise ``TooManyRequests`` when the window holds its limit already; count nothing.

        For the counters of outcomes (failed logins, not-found answers), which
        ``record`` counts once the outcome is known.
        """
        window = await self._count(rule, subject, hit=False)
        if window is not None and window.hits >= rule.limit:
            raise TooManyRequests(rule.name, window.resets_in_seconds)

    async def record(self, rule: RateLimitRule, subject: str) -> WindowCount | None:
        """Count an outcome; ``None`` when a rule that fails open could not count it."""
        return await self._count(rule, subject, hit=True)

    async def record_lookup_not_found(self, address: str) -> None:
        """Count a not-found lookup for its address and for everyone, and warn on a spike."""
        self.buyer_lookup_not_found_total += 1
        await self.record(self.rules.buyer_lookup_not_found, address)
        alert = self.rules.buyer_lookup_not_found_alert
        everyone = await self.record(alert, EVERYONE)
        # Once per window, by whichever process crosses the threshold first.
        if everyone is not None and everyone.hits == alert.limit + 1:
            logger.warning(
                "buyer_lookup_not_found_spike",
                threshold_per_minute=alert.limit,
                window_ends_in_seconds=everyone.resets_in_seconds,
            )

    async def _count(self, rule: RateLimitRule, subject: str, *, hit: bool) -> WindowCount | None:
        key = RateLimitKey(rule.name, subject)
        try:
            if hit:
                return await self._limiter.hit(key, window_seconds=rule.window_seconds)
            return await self._limiter.peek(key)
        except ServiceUnavailable:
            if rule.fail_closed:
                raise
            self.limiter_errors_total += 1
            logger.warning("rate_limiter_unavailable", rule=rule.name)
            return None


def rate_limiting(request: Request) -> RateLimiting:
    """The limits ``create_app`` put on the app's state."""
    limits = getattr(request.app.state, "rate_limiting", None)
    if not isinstance(limits, RateLimiting):
        raise RuntimeError("the app was built without rate limits")
    return limits


async def too_many_requests(_request: Request, error: Exception) -> JSONResponse:
    """The 429 of every limit: a fixed message, and when to try again."""
    if not isinstance(error, TooManyRequests):
        raise error
    logger.info("rate_limited", rule=error.rule)
    return JSONResponse(
        {"detail": TOO_MANY_REQUESTS},
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(error.retry_after_seconds)},
    )
