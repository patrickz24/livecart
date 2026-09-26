"""The seller auth journeys on the SQL adapters, argon2id and livecart_test.

The app runs as livecart_app over the SQL repositories, with a fake clock
that starts at the current time (PyJWT checks ``exp`` against the system
clock) and moves forward where a scenario needs the grace or the family to
end.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http.cookies import SimpleCookie
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.routes.auth import REFRESH_COOKIE
from livecart_api.testing import (
    TEST_ORIGIN,
    current_time_clock,
    make_settings,
    open_client,
    postgres_resources,
)
from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import SellerRecord
from livecart_core.application.seller_auth import REFRESH_FAMILY_LIFETIME
from livecart_core.infrastructure.db.tables import refresh_tokens
from livecart_core.testing.fakes import FakeClock

pytestmark = pytest.mark.integration

EMAIL = "Seller@Example.test"
PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


@dataclass
class PostgresApi:
    client: httpx2.AsyncClient
    clock: FakeClock
    seller: SellerRecord
    engine: AsyncEngine

    async def login(self, email: str = EMAIL, password: str = PASSWORD) -> httpx2.Response:
        return await self.client.post(
            LOGIN, json={"email": email, "password": password}, headers={"Origin": TEST_ORIGIN}
        )

    async def refresh(self, cookie: str) -> httpx2.Response:
        return await self.client.post(
            REFRESH, headers={"Origin": TEST_ORIGIN, "Cookie": f"{REFRESH_COOKIE}={cookie}"}
        )

    async def token_rows(self) -> list[tuple[bytes, UUID, object]]:
        """(token_hash, family_id, revoked_at) of every stored refresh token."""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(
                    refresh_tokens.c.token_hash,
                    refresh_tokens.c.family_id,
                    refresh_tokens.c.revoked_at,
                )
            )
            return [(row.token_hash, row.family_id, row.revoked_at) for row in rows]


@pytest.fixture
async def api(clean_database: None, app_engine: AsyncEngine) -> AsyncIterator[PostgresApi]:
    clock = current_time_clock()
    resources = postgres_resources(app_engine, clock=clock)
    seller = await CreateSeller(resources.sellers, resources.passwords, clock)(
        email=EMAIL, slug="demo-seller", display_name="Demo", password=PASSWORD
    )
    async with open_client(create_app(make_settings(), resources)) as client:
        yield PostgresApi(client, clock, seller, app_engine)


def _refresh_cookie(response: httpx2.Response) -> str:
    (header,) = response.headers.get_list("set-cookie")
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(header)
    return cookie[REFRESH_COOKIE].value


async def test_a_login_stores_the_argon2id_hash_and_the_token_hash_only(api: PostgresApi) -> None:
    response = await api.login()
    me = await api.client.get(
        ME, headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )

    assert response.status_code == 200
    assert me.status_code == 200
    assert me.json()["id"] == str(api.seller.id)
    assert api.seller.password_hash.startswith("$argon2id$")
    assert PASSWORD not in api.seller.password_hash
    cookie = _refresh_cookie(response)
    ((token_hash, _, revoked_at),) = await api.token_rows()
    assert token_hash == hashlib.sha256(cookie.encode()).digest()
    assert revoked_at is None


async def test_an_email_is_matched_whatever_its_case(api: PostgresApi) -> None:
    assert (await api.login("seller@EXAMPLE.test")).status_code == 200


async def test_an_unknown_email_and_a_wrong_password_get_the_same_401(api: PostgresApi) -> None:
    unknown = await api.login("nobody@example.test")
    wrong = await api.login(password="not the right password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert await api.token_rows() == []


async def test_two_concurrent_refreshes_give_one_rotation_and_one_access_only_answer(
    api: PostgresApi,
) -> None:
    for _ in range(3):
        cookie = _refresh_cookie(await api.login())

        responses = await asyncio.gather(api.refresh(cookie), api.refresh(cookie))

        assert [response.status_code for response in responses] == [200, 200]
        rotated = [response for response in responses if "set-cookie" in response.headers]
        assert len(rotated) == 1
        # The jar holds the one successor that was set, and it refreshes in turn.
        assert api.client.cookies.get(REFRESH_COOKIE) == _refresh_cookie(rotated[0])
        again = await api.client.post(REFRESH, headers={"Origin": TEST_ORIGIN})
        assert again.status_code == 200
        assert "set-cookie" in again.headers


async def test_a_reuse_after_thirty_seconds_revokes_the_family(api: PostgresApi) -> None:
    first = _refresh_cookie(await api.login())
    other = _refresh_cookie(await api.login())
    second = _refresh_cookie(await api.refresh(first))
    api.clock.advance(31)

    replayed = await api.refresh(first)
    successor = await api.refresh(second)

    assert replayed.status_code == successor.status_code == 401
    rows = await api.token_rows()
    other_hash = hashlib.sha256(other.encode()).digest()
    ((other_family, other_revoked),) = [
        (family, revoked) for token_hash, family, revoked in rows if token_hash == other_hash
    ]
    assert other_revoked is None
    family = [revoked for _, family_id, revoked in rows if family_id != other_family]
    assert len(family) == 2
    assert None not in family
    assert (await api.refresh(other)).status_code == 200


async def test_an_expired_refresh_token_is_refused(api: PostgresApi) -> None:
    cookie = _refresh_cookie(await api.login())
    api.clock.advance(REFRESH_FAMILY_LIFETIME)

    assert (await api.refresh(cookie)).status_code == 401


async def test_a_logout_revokes_the_family_in_the_database(api: PostgresApi) -> None:
    await api.login()
    await api.client.post(REFRESH, headers={"Origin": TEST_ORIGIN})

    response = await api.client.post(LOGOUT, headers={"Origin": TEST_ORIGIN})

    assert response.status_code == 204
    revoked = [revoked_at for _, _, revoked_at in await api.token_rows()]
    assert len(revoked) == 2
    assert None not in revoked
