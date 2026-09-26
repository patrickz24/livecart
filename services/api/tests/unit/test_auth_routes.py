"""The seller auth routes over the in-memory fakes: cookies, grace, origins and bearer tokens.

``tests/integration/test_auth_postgres.py`` runs the same journeys on the SQL
adapters and argon2id.
"""

import asyncio
import base64
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI

from livecart_api.access_tokens import AUDIENCE, ISSUER, AccessTokens
from livecart_api.app import create_app
from livecart_api.deps import parse_origin
from livecart_api.routes.auth import REFRESH_COOKIE
from livecart_api.testing import (
    TEST_ORIGIN,
    current_time_clock,
    make_app,
    make_settings,
    memory_resources,
    open_client,
)
from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.seller_auth import REFRESH_FAMILY_LIFETIME, hash_refresh_token
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import (
    FAKE_DUMMY_HASH,
    FakeClock,
    FakePasswordHasher,
    InMemoryDatabase,
)
from livecart_core.testing.keys import TEST_JWT_SECRET

EMAIL = "seller@example.test"
PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
SAME_ORIGIN = {"Origin": TEST_ORIGIN}


@dataclass
class Api:
    app: FastAPI
    database: InMemoryDatabase
    clock: FakeClock
    passwords: FakePasswordHasher
    seller: SellerRecord


async def _api(app_env: AppEnv = AppEnv.DEVELOPMENT) -> Api:
    database, clock, passwords = InMemoryDatabase(), current_time_clock(), FakePasswordHasher()
    seller = await CreateSeller(database.sellers, passwords, clock)(
        email=EMAIL, slug="demo-seller", display_name="Demo", password=PASSWORD
    )
    passwords.verified.clear()
    app = make_app(database=database, passwords=passwords, clock=clock, app_env=app_env)
    return Api(app, database, clock, passwords, seller)


@pytest.fixture
async def api() -> Api:
    return await _api()


def _set_cookies(response: httpx2.Response) -> list[SimpleCookie]:
    cookies = []
    for header in response.headers.get_list("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        cookies.append(cookie)
    return cookies


def _refresh_cookie(response: httpx2.Response) -> str:
    """The value of the one lc_refresh cookie the response sets."""
    (cookie,) = _set_cookies(response)
    return cookie[REFRESH_COOKIE].value


async def _login(client: httpx2.AsyncClient) -> httpx2.Response:
    response = await client.post(
        LOGIN, json={"email": EMAIL, "password": PASSWORD}, headers=SAME_ORIGIN
    )
    assert response.status_code == 200
    return response


async def _refresh(client: httpx2.AsyncClient, cookie: str) -> httpx2.Response:
    """A refresh that sends ``cookie``, whatever the client's jar holds."""
    return await client.post(
        REFRESH, headers={**SAME_ORIGIN, "Cookie": f"{REFRESH_COOKIE}={cookie}"}
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Login -----------------------------------------------------------------------------


async def test_a_login_answers_an_access_token_and_sets_the_refresh_cookie(api: Api) -> None:
    async with open_client(api.app) as client:
        response = await _login(client)
        me = await client.get(ME, headers=_bearer(response.json()["access_token"]))

    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert (body["token_type"], body["expires_in"]) == ("bearer", 900)
    assert response.headers["Cache-Control"] == "no-store"
    (cookie,) = _set_cookies(response)
    morsel = cookie[REFRESH_COOKIE]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["path"] == "/api/v1/auth"
    assert morsel["max-age"] == str(int(REFRESH_FAMILY_LIFETIME.total_seconds()))
    assert not morsel["secure"]
    # Only the SHA-256 of the cookie's token is stored.
    stored = await api.database.refresh_tokens.find(hash_refresh_token(morsel.value))
    assert stored is not None
    assert stored.seller_id == api.seller.id
    assert me.status_code == 200
    assert me.json() == {
        "id": str(api.seller.id),
        "email": EMAIL,
        "slug": "demo-seller",
        "display_name": "Demo",
    }


async def test_the_refresh_cookie_is_secure_in_production() -> None:
    api = await _api(AppEnv.PRODUCTION)

    async with open_client(api.app) as client:
        response = await _login(client)

    (cookie,) = _set_cookies(response)
    assert cookie[REFRESH_COOKIE]["secure"] is True


async def test_an_unknown_email_and_a_wrong_password_get_the_same_answer(api: Api) -> None:
    async with open_client(api.app) as client:
        unknown = await client.post(
            LOGIN, json={"email": "nobody@example.test", "password": "x"}, headers=SAME_ORIGIN
        )
        wrong = await client.post(
            LOGIN, json={"email": EMAIL, "password": "wrong password"}, headers=SAME_ORIGIN
        )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "Invalid e-mail or password"}
    assert "set-cookie" not in unknown.headers
    assert "set-cookie" not in wrong.headers
    # The same path: one password check each, the unknown e-mail against the dummy.
    assert api.passwords.verified == [FAKE_DUMMY_HASH, api.seller.password_hash]


async def test_the_email_of_a_login_ignores_case_and_surrounding_spaces(api: Api) -> None:
    async with open_client(api.app) as client:
        response = await client.post(
            LOGIN,
            json={"email": "  SELLER@example.TEST ", "password": PASSWORD},
            headers=SAME_ORIGIN,
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"email": EMAIL},
        {"password": PASSWORD},
        {"email": EMAIL, "password": PASSWORD, "remember_me": True},
        {"email": EMAIL, "password": "x" * 129},
        {"email": EMAIL, "password": ""},
        {"email": "x" * 255, "password": PASSWORD},
    ],
)
async def test_a_login_body_outside_its_schema_is_refused(
    api: Api, body: dict[str, object]
) -> None:
    async with open_client(api.app) as client:
        response = await client.post(LOGIN, json=body, headers=SAME_ORIGIN)

    assert response.status_code == 422
    assert api.passwords.verified == []
    # No error repeats what it refused: a missing or extra field would echo the whole body.
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) <= {"type", "loc", "msg", "ctx"} for error in errors)
    password = str(body.get("password", ""))
    assert not password or password not in response.text


async def test_a_password_too_long_is_refused_without_being_repeated(api: Api) -> None:
    password = "P" * 200

    async with open_client(api.app) as client:
        response = await client.post(
            LOGIN, json={"email": EMAIL, "password": password}, headers=SAME_ORIGIN
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "too_long",
                "loc": ["body", "password"],
                "msg": "Value should have at most 128 items after validation, not 200",
                "ctx": {"field_type": "Value", "max_length": 128, "actual_length": 200},
            }
        ]
    }
    assert password not in response.text


@pytest.mark.parametrize("origin", [None, "null", "http://evil.test", "https://test"])
async def test_a_login_from_another_origin_is_refused_before_any_password_check(
    api: Api, origin: str | None
) -> None:
    headers = {} if origin is None else {"Origin": origin}
    async with open_client(api.app) as client:
        response = await client.post(
            LOGIN, json={"email": EMAIL, "password": PASSWORD}, headers=headers
        )

    # A page of another site cannot sign the browser into an account of its choosing.
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request refused"}
    assert "set-cookie" not in response.headers
    assert api.passwords.verified == []
    assert api.database.tables.refresh_tokens == {}


# --- Refresh ---------------------------------------------------------------------------


async def test_a_refresh_rotates_the_cookie(api: Api) -> None:
    async with open_client(api.app) as client:
        first = _refresh_cookie(await _login(client))
        response = await _refresh(client, first)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    second = _refresh_cookie(response)
    assert second != first
    used = await api.database.refresh_tokens.find(hash_refresh_token(first))
    assert used is not None
    assert used.used_at == api.clock.now()


async def test_two_concurrent_refreshes_give_one_rotation_and_the_same_cookie(api: Api) -> None:
    async with open_client(api.app) as client:
        cookie = _refresh_cookie(await _login(client))
        # Two tabs read the shared jar at once, so both send the same cookie.
        responses = await asyncio.gather(_refresh(client, cookie), _refresh(client, cookie))
        rotated = [response for response in responses if "set-cookie" in response.headers]
        jar = client.cookies.get(REFRESH_COOKIE)
        after = await client.post(REFRESH, headers=SAME_ORIGIN)

    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["access_token"] for response in responses)
    # One answer rotated and set the successor; the other carried an access token only.
    assert len(rotated) == 1
    assert jar == _refresh_cookie(rotated[0])
    # The jar that both tabs share still works.
    assert after.status_code == 200


async def test_a_reuse_inside_the_grace_answers_without_a_cookie(api: Api) -> None:
    async with open_client(api.app) as client:
        first = _refresh_cookie(await _login(client))
        await _refresh(client, first)
        api.clock.advance(29)
        again = await _refresh(client, first)

    assert again.status_code == 200
    assert again.json()["access_token"]
    assert "set-cookie" not in again.headers


async def test_a_reuse_after_the_grace_revokes_the_family(api: Api) -> None:
    async with open_client(api.app) as client:
        first = _refresh_cookie(await _login(client))
        second = _refresh_cookie(await _refresh(client, first))
        api.clock.advance(31)
        replayed = await _refresh(client, first)
        successor = await _refresh(client, second)

    assert replayed.status_code == successor.status_code == 401
    assert replayed.json() == {"detail": "Invalid refresh token"}
    (cleared,) = _set_cookies(replayed)
    assert cleared[REFRESH_COOKIE]["max-age"] == "0"
    assert all(row.revoked_at is not None for row in api.database.tables.refresh_tokens.values())


async def test_an_expired_refresh_cookie_is_refused(api: Api) -> None:
    async with open_client(api.app) as client:
        cookie = _refresh_cookie(await _login(client))
        api.clock.advance(REFRESH_FAMILY_LIFETIME)
        response = await _refresh(client, cookie)

    assert response.status_code == 401


@pytest.mark.parametrize("cookie", [None, "not-a-token", "A" * 43])
async def test_a_refresh_without_a_valid_cookie_is_refused(api: Api, cookie: str | None) -> None:
    async with open_client(api.app) as client:
        if cookie is None:
            response = await client.post(REFRESH, headers=SAME_ORIGIN)
        else:
            response = await _refresh(client, cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid refresh token"}


@pytest.mark.parametrize(
    "origin",
    [None, "null", "http://evil.test", "https://test", "http://test:8080", "http://test/path"],
)
async def test_a_refresh_from_another_origin_is_refused_before_any_rotation(
    api: Api, origin: str | None
) -> None:
    headers = {} if origin is None else {"Origin": origin}
    async with open_client(api.app) as client:
        cookie = _refresh_cookie(await _login(client))
        response = await client.post(REFRESH, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request refused"}
    token = await api.database.refresh_tokens.find(hash_refresh_token(cookie))
    assert token is not None
    assert token.is_active(api.clock.now())


async def test_the_default_port_may_be_spelled_out_in_the_origin(api: Api) -> None:
    async with open_client(api.app) as client:
        await _login(client)
        response = await client.post(REFRESH, headers={"Origin": "http://TEST:80"})

    assert response.status_code == 200


# --- Logout ----------------------------------------------------------------------------


async def test_a_logout_revokes_the_family_and_clears_the_cookie(api: Api) -> None:
    async with open_client(api.app) as client:
        first = _refresh_cookie(await _login(client))
        second = _refresh_cookie(await _refresh(client, first))
        response = await client.post(LOGOUT, headers=SAME_ORIGIN)
        after = await _refresh(client, second)

    assert response.status_code == 204
    (cleared,) = _set_cookies(response)
    assert cleared[REFRESH_COOKIE]["max-age"] == "0"
    assert cleared[REFRESH_COOKIE]["path"] == "/api/v1/auth"
    assert after.status_code == 401
    assert all(row.revoked_at is not None for row in api.database.tables.refresh_tokens.values())


async def test_a_logout_without_a_cookie_still_answers_204(api: Api) -> None:
    async with open_client(api.app) as client:
        response = await client.post(LOGOUT, headers=SAME_ORIGIN)

    assert response.status_code == 204


async def test_a_logout_from_another_origin_revokes_nothing(api: Api) -> None:
    async with open_client(api.app) as client:
        await _login(client)
        response = await client.post(LOGOUT, headers={"Origin": "http://evil.test"})

    assert response.status_code == 403
    assert all(row.revoked_at is None for row in api.database.tables.refresh_tokens.values())


# --- CurrentSeller ---------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic c2VsbGVyOnB3"}])
async def test_me_without_a_bearer_token_is_401_with_a_challenge(
    api: Api, headers: dict[str, str]
) -> None:
    async with open_client(api.app) as client:
        response = await client.get(ME, headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": "Not authenticated"}


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _unsigned(seller_id: UUID) -> str:
    """An alg: none token with every claim right, as an attacker would forge it."""
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": str(seller_id), "exp": 4102444800, "jti": "x"}
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."


async def test_me_refuses_a_forged_expired_or_orphan_token(api: Api) -> None:
    stale_clock = current_time_clock()
    stale_clock.set(stale_clock.now() - timedelta(minutes=16))
    expired = AccessTokens(TEST_JWT_SECRET, stale_clock).issue(api.seller.id).value
    orphan = AccessTokens(TEST_JWT_SECRET, api.clock).issue(UUID(int=404)).value

    async with open_client(api.app) as client:
        responses = [
            await client.get(ME, headers=_bearer(token))
            for token in ("garbage", _unsigned(api.seller.id), expired, orphan)
        ]

    assert [response.status_code for response in responses] == [401] * 4
    assert {response.headers["WWW-Authenticate"] for response in responses} == {
        'Bearer error="invalid_token"'
    }


# --- Failures and contract -------------------------------------------------------------


class UnavailableSellers:
    """A seller repository whose database is down, as the SQL units report it."""

    async def add(
        self, *, email: str, slug: str, display_name: str, password_hash: str, now: datetime
    ) -> SellerRecord:
        raise ServiceUnavailable("database unavailable: TimeoutError")

    async def get(self, seller_id: UUID) -> SellerRecord | None:
        raise ServiceUnavailable("database unavailable: TimeoutError")

    async def find_by_email(self, email: str) -> SellerRecord | None:
        raise ServiceUnavailable("database unavailable: TimeoutError")

    async def find_by_slug(self, slug: str) -> SellerRecord | None:
        raise ServiceUnavailable("database unavailable: TimeoutError")


async def test_a_database_outage_answers_503_with_retry_after() -> None:
    resources = replace(memory_resources(InMemoryDatabase()), sellers=UnavailableSellers())

    async with open_client(create_app(make_settings(), resources)) as client:
        response = await client.post(
            LOGIN, json={"email": EMAIL, "password": PASSWORD}, headers=SAME_ORIGIN
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {"detail": "Service temporarily unavailable"}


def test_openapi_documents_the_auth_routes_and_the_bearer_scheme() -> None:
    schema = make_app(app_env=AppEnv.PRODUCTION).openapi()

    paths = schema["paths"]
    assert paths[LOGIN]["post"]["operationId"] == "login"
    assert paths[REFRESH]["post"]["operationId"] == "refresh_access_token"
    assert paths[LOGOUT]["post"]["operationId"] == "logout"
    assert paths[ME]["get"]["operationId"] == "get_current_seller"
    assert paths[ME]["get"]["security"] == [{"SellerAccessToken": []}]
    assert schema["components"]["securitySchemes"]["SellerAccessToken"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "The access_token of POST /api/v1/auth/login or /api/v1/auth/refresh.",
    }
    assert set(paths[LOGIN]["post"]["responses"]) >= {"200", "401", "403", "429", "503"}
    assert set(paths[REFRESH]["post"]["responses"]) >= {"200", "401", "403"}
    assert set(paths[LOGOUT]["post"]["responses"]) >= {"204", "403"}
    # The password is write-only, and no route takes the cookie as a parameter.
    login_schema = schema["components"]["schemas"]["LoginRequest"]["properties"]["password"]
    assert login_schema["writeOnly"] is True
    assert "parameters" not in paths[REFRESH]["post"]


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        ("http://localhost:8080", ("http", "localhost", 8080)),
        ("https://LiveCart.example", ("https", "livecart.example", 443)),
        ("http://127.0.0.1", ("http", "127.0.0.1", 80)),
        ("http://[::1]:8000/", ("http", "::1", 8000)),
        ("null", None),
        ("file:///etc/passwd", None),
        ("http://user@localhost", None),
        ("http://localhost:99999", None),
        ("http://localhost/?q", None),
        ("http://localhost/#f", None),
    ],
)
def test_an_origin_is_read_as_scheme_host_and_port(
    value: str, origin: tuple[str, str, int] | None
) -> None:
    assert parse_origin(value) == origin
