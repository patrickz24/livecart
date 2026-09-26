"""The rate limits of the routes over the fakes: 429 with Retry-After, and a Redis outage.

The limits are lowered through the settings, so that each test reaches one in
a few requests; the fake limiter's windows are timed by the app's clock.
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID

import httpx2
import pytest
import structlog
from fastapi import FastAPI

from livecart_api.rate_limits import (
    TOO_MANY_REQUESTS,
    UNKNOWN_CLIENT,
    RateLimiting,
    RateLimitRules,
    group_address,
    login_failure_subject,
    token_subject,
)
from livecart_api.routes.auth import REFRESH_COOKIE
from livecart_api.testing import (
    TEST_ORIGIN,
    current_time_clock,
    make_app,
    make_settings,
    open_client,
    open_order,
    seller_headers,
)
from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import OrderRecord, RateLimitKey
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import (
    TEST_ORDER_TOKENS,
    FakeClock,
    FakePasswordHasher,
    InMemoryDatabase,
    InMemoryRateLimiter,
)

EMAIL = "seller@example.test"
PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
SESSIONS = "/api/v1/live-sessions"
STOREFRONT = "/api/v1/storefronts/demo-seller"
ORDERS = "/api/v1/orders"
TOO_MANY = {"detail": TOO_MANY_REQUESTS}
UNKNOWN_REFERENCE = "LC-ZZZZZZ"


@dataclass
class Api:
    app: FastAPI
    database: InMemoryDatabase
    clock: FakeClock
    limiter: InMemoryRateLimiter
    seller_id: UUID

    @property
    def limits(self) -> RateLimiting:
        limits = self.app.state.rate_limiting
        assert isinstance(limits, RateLimiting)
        return limits


async def _api(**limits: int) -> Api:
    database, clock, passwords = InMemoryDatabase(), current_time_clock(), FakePasswordHasher()
    seller = await CreateSeller(database.sellers, passwords, clock)(
        email=EMAIL, slug="demo-seller", display_name="Demo", password=PASSWORD
    )
    limiter = InMemoryRateLimiter(clock)
    app = make_app(
        database=database,
        passwords=passwords,
        clock=clock,
        rate_limiter=limiter,
        settings=make_settings(AppEnv.DEVELOPMENT, **limits),
    )
    return Api(app, database, clock, limiter, seller.id)


async def _login(
    client: httpx2.AsyncClient, *, email: str = EMAIL, password: str = PASSWORD
) -> httpx2.Response:
    return await client.post(
        LOGIN, json={"email": email, "password": password}, headers={"Origin": TEST_ORIGIN}
    )


def _assert_too_many(response: httpx2.Response, retry_after: int) -> None:
    assert response.status_code == 429
    assert response.json() == TOO_MANY
    assert response.headers["Retry-After"] == str(retry_after)


# --- Keys ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "subject"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::ffff", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("fe80::1%eth0", "fe80::/64"),
        ("testclient", UNKNOWN_CLIENT),
        (None, UNKNOWN_CLIENT),
    ],
)
def test_ipv4_addresses_count_alone_and_ipv6_ones_by_their_slash_64(
    host: str | None, subject: str
) -> None:
    assert group_address(host) == subject


def test_the_account_of_a_login_counts_by_a_hash_whatever_the_case() -> None:
    subject = login_failure_subject("Seller@Example.TEST", "203.0.113.7")

    assert subject == login_failure_subject(EMAIL, "203.0.113.7")
    assert subject != login_failure_subject(EMAIL, "198.51.100.1")
    assert "example" not in subject.lower()
    assert subject.endswith(":203.0.113.7")


def test_a_token_counts_by_its_sha256() -> None:
    assert token_subject("secret-token") == hashlib.sha256(b"secret-token").hexdigest()


def test_the_rules_follow_the_settings_and_only_the_login_fails_closed() -> None:
    rules = RateLimitRules.from_settings(make_settings())

    assert [
        (rule.name, rule.limit, rule.window_seconds, rule.fail_closed)
        for rule in (
            rules.login,
            rules.login_failures,
            rules.seller,
            rules.buyer_lookup,
            rules.buyer_lookup_not_found,
            rules.buyer_lookup_not_found_alert,
            rules.buyer_token,
            rules.storefront,
            rules.comment_webhook,
            rules.comment_webhook_signature_failures,
            rules.payment_webhook_signature_failures,
        )
    ] == [
        ("login", 10, 60, True),
        ("login_failures", 5, 900, True),
        ("seller", 300, 60, False),
        ("buyer_lookup", 30, 60, False),
        ("buyer_lookup_not_found", 20, 60, False),
        ("buyer_lookup_not_found_all", 100, 60, False),
        ("buyer_token", 60, 60, False),
        ("storefront", 120, 60, False),
        ("comment_webhook", 6000, 60, False),
        ("comment_webhook_signature_failures", 60, 60, False),
        ("payment_webhook_signature_failures", 60, 60, False),
    ]


# --- Login -----------------------------------------------------------------------------


async def test_logins_are_limited_per_client_address() -> None:
    api = await _api(rate_limit_login_per_minute=2)

    async with open_client(api.app, peer="203.0.113.7") as client:
        allowed = [(await _login(client)).status_code for _ in range(2)]
        refused = await _login(client)
        api.clock.advance(20)
        # A malformed body without an Origin counts too: the limit comes before either check.
        malformed = await client.post(LOGIN, json={"email": EMAIL})
    async with open_client(api.app, peer="198.51.100.1") as other:
        elsewhere = await _login(other)

    assert allowed == [200, 200]
    _assert_too_many(refused, 60)
    _assert_too_many(malformed, 40)
    assert elsewhere.status_code == 200


async def test_a_new_window_lets_logins_in_again() -> None:
    api = await _api(rate_limit_login_per_minute=1)

    async with open_client(api.app) as client:
        await _login(client)
        refused = await _login(client)
        api.clock.advance(60)
        again = await _login(client)

    assert refused.status_code == 429
    assert again.status_code == 200


async def test_failed_logins_lock_the_account_for_that_address_only() -> None:
    api = await _api(rate_limit_login_failures=3)
    await CreateSeller(api.database.sellers, FakePasswordHasher(), api.clock)(
        email="other@example.test", slug="other-shop", display_name="Other", password=PASSWORD
    )

    async with open_client(api.app, peer="203.0.113.7") as client:
        failures = [
            (await _login(client, password="wrong password!")).status_code for _ in range(3)
        ]
        # Past the limit, the right password is refused too: guessing gains nothing.
        right = await _login(client, email=EMAIL.upper())
        other_account = await _login(client, email="other@example.test")
    async with open_client(api.app, peer="198.51.100.1") as elsewhere:
        # Failing on purpose from another address never locks the seller out.
        from_elsewhere = await _login(elsewhere)

    assert failures == [401, 401, 401]
    _assert_too_many(right, 900)
    assert other_account.status_code == 200
    assert from_elsewhere.status_code == 200


async def test_successful_logins_are_no_failures() -> None:
    api = await _api(rate_limit_login_failures=1)

    async with open_client(api.app) as client:
        statuses = [(await _login(client)).status_code for _ in range(3)]
        failure = await _login(client, password="wrong password!")
        locked = await _login(client)

    assert statuses == [200, 200, 200]
    assert failure.status_code == 401
    assert locked.status_code == 429


# --- Seller routes ---------------------------------------------------------------------


async def test_seller_routes_are_limited_per_seller() -> None:
    api = await _api(rate_limit_seller_per_minute=2)
    other = await CreateSeller(api.database.sellers, FakePasswordHasher(), api.clock)(
        email="other@example.test", slug="other-shop", display_name="Other", password=PASSWORD
    )

    async with open_client(api.app) as client:
        # Refused tokens reach no counter: only the seller's own requests fill it.
        anonymous = [(await client.get(SESSIONS)).status_code for _ in range(3)]
        allowed = [
            (await client.get(SESSIONS, headers=seller_headers(api.app, api.seller_id))).status_code
            for _ in range(2)
        ]
        refused = await client.get(
            "/api/v1/auth/me", headers=seller_headers(api.app, api.seller_id)
        )
        other_seller = await client.get(SESSIONS, headers=seller_headers(api.app, other.id))

    assert anonymous == [401, 401, 401]
    assert allowed == [200, 200]
    _assert_too_many(refused, 60)
    assert other_seller.status_code == 200


# --- Buyer lookups ---------------------------------------------------------------------


async def _order(api: Api) -> OrderRecord:
    session = api.database.add_live_session(seller_id=api.seller_id)
    api.database.add_product(session.id, "B07", name="Sac en cuir", stock_total=5)
    return await open_order(api.database, session.id, "B07")


async def test_lookups_by_reference_are_limited_per_client_address() -> None:
    api = await _api(rate_limit_buyer_lookup_per_minute=2)
    order = await _order(api)
    path = f"{ORDERS}/{order.reference.value}"

    async with open_client(api.app, peer="203.0.113.7") as client:
        allowed = [(await client.get(path)).status_code for _ in range(2)]
        refused = await client.get(path)
    async with open_client(api.app, peer="198.51.100.1") as other:
        elsewhere = await other.get(path)

    assert allowed == [200, 200]
    _assert_too_many(refused, 60)
    assert elsewhere.status_code == 200


async def test_not_found_lookups_stop_an_address_from_finding_anything() -> None:
    api = await _api(rate_limit_buyer_lookup_not_found_per_minute=2)
    order = await _order(api)

    async with open_client(api.app, peer="203.0.113.7") as client:
        guesses = [
            (await client.get(f"{ORDERS}/{UNKNOWN_REFERENCE}")).status_code for _ in range(2)
        ]
        # Past its not-found answers, the address finds nothing, a real order included.
        real = await client.get(f"{ORDERS}/{order.reference.value}")
    async with open_client(api.app, peer="198.51.100.1") as other:
        elsewhere = await other.get(f"{ORDERS}/{order.reference.value}")

    assert guesses == [404, 404]
    _assert_too_many(real, 60)
    assert elsewhere.status_code == 200
    assert api.limits.buyer_lookup_not_found_total == 2


async def test_not_found_lookups_from_every_address_warn_once_past_the_threshold() -> None:
    api = await _api(buyer_lookup_not_found_alert_per_minute=3)

    with structlog.testing.capture_logs() as logs:
        for n in range(5):
            async with open_client(api.app, peer=f"203.0.113.{n + 1}") as client:
                assert (await client.get(f"{ORDERS}/{UNKNOWN_REFERENCE}")).status_code == 404

    spikes = [log for log in logs if log["event"] == "buyer_lookup_not_found_spike"]
    assert spikes == [
        {
            "event": "buyer_lookup_not_found_spike",
            "log_level": "warning",
            "threshold_per_minute": 3,
            "window_ends_in_seconds": 60,
        }
    ]
    assert api.limits.buyer_lookup_not_found_total == 5


async def test_token_routes_are_limited_per_token_or_else_per_address() -> None:
    api = await _api(rate_limit_buyer_token_per_minute=1)
    order = await _order(api)
    details = f"{ORDERS}/{order.reference.value}/details"
    token = {"Authorization": f"Bearer {TEST_ORDER_TOKENS.token_for(order.id)}"}
    other_token = {"Authorization": "Bearer another-token-entirely"}

    async with open_client(api.app) as client:
        first = await client.get(details, headers=token)
        second = await client.get(details, headers=token)
        # Another token has a counter of its own; its 404 counts against the address.
        other = await client.get(details, headers=other_token)
        tokenless = [(await client.get(details)).status_code for _ in range(2)]

    assert first.status_code == 200
    _assert_too_many(second, 60)
    assert other.status_code == 404
    assert tokenless == [404, 429]
    assert api.limits.buyer_lookup_not_found_total == 2


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_new_token_value_opens_no_new_window_for_its_address() -> None:
    api = await _api(rate_limit_buyer_lookup_not_found_per_minute=2)
    order = await _order(api)
    details = f"{ORDERS}/{order.reference.value}/details"
    token = _bearer(TEST_ORDER_TOKENS.token_for(order.id))

    async with open_client(api.app, peer="203.0.113.7") as client:
        guesses = [
            (await client.get(details, headers=_bearer(f"forged-{n}"))).status_code
            for n in range(2)
        ]
        fresh = await client.get(details, headers=_bearer("forged-2"))
        # The address shares its not-found answers with the lookup: past them, nothing is found.
        real = await client.get(details, headers=token)
        lookup = await client.get(f"{ORDERS}/{order.reference.value}")
    async with open_client(api.app, peer="198.51.100.1") as other:
        elsewhere = await other.get(details, headers=token)

    assert guesses == [404, 404]
    _assert_too_many(fresh, 60)
    _assert_too_many(real, 60)
    _assert_too_many(lookup, 60)
    assert elsewhere.status_code == 200
    # A refused request opened no counter for the token it sent.
    assert RateLimitKey("buyer_token", token_subject("forged-0")) in api.limiter.windows
    assert RateLimitKey("buyer_token", token_subject("forged-2")) not in api.limiter.windows


async def test_a_new_token_value_per_request_is_stopped_at_the_default_limits() -> None:
    api = await _api()
    order = await _order(api)
    details = f"{ORDERS}/{order.reference.value}/details"

    async with open_client(api.app, peer="203.0.113.7") as client:
        statuses = [
            (await client.get(details, headers=_bearer(f"forged-{n}"))).status_code
            for n in range(65)
        ]

    assert statuses == [404] * 20 + [429] * 45


async def test_the_verified_tokens_of_one_address_count_per_token_only() -> None:
    api = await _api(
        rate_limit_buyer_lookup_not_found_per_minute=1, rate_limit_buyer_token_per_minute=3
    )
    session = api.database.add_live_session(seller_id=api.seller_id)
    api.database.add_product(session.id, "B07", name="Sac en cuir", stock_total=5)
    orders = [
        await open_order(api.database, session.id, "B07", buyer=f"buyer-{n}") for n in range(3)
    ]

    # A whole audience behind one carrier's address (CGNAT), each buyer with its own token.
    async with open_client(api.app, peer="203.0.113.7") as client:
        statuses = [
            (
                await client.get(
                    f"{ORDERS}/{order.reference.value}/details",
                    headers=_bearer(TEST_ORDER_TOKENS.token_for(order.id)),
                )
            ).status_code
            for order in orders
            for _ in range(3)
        ]

    assert statuses == [200] * 9
    assert api.limits.buyer_lookup_not_found_total == 0


# --- Storefront ------------------------------------------------------------------------


async def test_storefronts_are_limited_per_client_address() -> None:
    api = await _api(rate_limit_storefront_per_minute=2)

    async with open_client(api.app, peer="203.0.113.7") as client:
        allowed = [(await client.get(STOREFRONT)).status_code for _ in range(2)]
        # Another slug, an unknown one included, counts in the same window.
        refused = await client.get("/api/v1/storefronts/unknown-shop")
    async with open_client(api.app, peer="198.51.100.1") as other:
        elsewhere = await other.get(STOREFRONT)

    assert allowed == [200, 200]
    _assert_too_many(refused, 60)
    assert elsewhere.status_code == 200


# --- Redis outage ----------------------------------------------------------------------


async def test_without_the_counters_logins_close_and_the_refresh_stays_open() -> None:
    api = await _api()
    async with open_client(api.app) as client:
        signed_in = await _login(client)
        cookie = signed_in.cookies[REFRESH_COOKIE]
        api.limiter.fail_next(10)

        login = await _login(client)
        refresh = await client.post(
            REFRESH, headers={"Origin": TEST_ORIGIN, "Cookie": f"{REFRESH_COOKIE}={cookie}"}
        )

    assert login.status_code == 503
    assert login.headers["Retry-After"] == "1"
    assert refresh.status_code == 200
    assert refresh.json()["access_token"]


async def test_without_the_counters_other_routes_pass_and_the_failure_is_counted() -> None:
    api = await _api()
    order = await _order(api)
    api.limiter.fail_next(10)

    with structlog.testing.capture_logs() as logs:
        async with open_client(api.app) as client:
            seller = await client.get(SESSIONS, headers=seller_headers(api.app, api.seller_id))
            lookup = await client.get(f"{ORDERS}/{order.reference.value}")
            details = await client.get(
                f"{ORDERS}/{order.reference.value}/details",
                headers={"Authorization": f"Bearer {TEST_ORDER_TOKENS.token_for(order.id)}"},
            )
            storefront = await client.get(STOREFRONT)

    assert (seller.status_code, lookup.status_code, details.status_code) == (200, 200, 200)
    assert storefront.status_code == 200
    # One per counter read: the seller's, the lookup's two, the details' two, the storefront's.
    assert api.limits.limiter_errors_total == 6
    assert {log["rule"] for log in logs if log["event"] == "rate_limiter_unavailable"} == {
        "seller",
        "buyer_lookup",
        "buyer_lookup_not_found",
        "buyer_token",
        "storefront",
    }


def test_the_openapi_documents_the_429_of_every_limited_route() -> None:
    schema = make_app().openapi()

    limited = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "429" in operation["responses"]
    }
    assert ("POST", LOGIN) in limited
    assert ("GET", f"{ORDERS}/{{reference}}") in limited
    assert ("GET", f"{ORDERS}/{{reference}}/details") in limited
    assert ("GET", "/api/v1/storefronts/{seller_slug}") in limited
    assert ("POST", REFRESH) not in limited
    assert {("GET", SESSIONS), ("GET", "/api/v1/auth/me")} <= limited
    assert "503" in schema["paths"][LOGIN]["post"]["responses"]
