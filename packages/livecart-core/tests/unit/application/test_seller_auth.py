import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import IssuedRefreshToken, SellerGrant, SellerRecord
from livecart_core.application.seller_auth import (
    REFRESH_FAMILY_LIFETIME,
    REFRESH_TOKEN_LENGTH,
    REUSE_GRACE,
    AuthenticateSeller,
    AuthenticationFailed,
    InvalidCredentials,
    RefreshRefusal,
    RefreshRefused,
    RevokeRefreshFamily,
    RotateRefreshToken,
    hash_refresh_token,
    new_refresh_token,
    read_refresh_token,
)
from livecart_core.testing.fakes import (
    FAKE_DUMMY_HASH,
    FakeClock,
    FakePasswordHasher,
    InMemoryDatabase,
)

EMAIL = "seller@example.test"
PASSWORD = "correct horse battery staple"


@dataclass
class World:
    database: InMemoryDatabase
    clock: FakeClock
    passwords: FakePasswordHasher
    seller: SellerRecord

    @property
    def authenticate(self) -> AuthenticateSeller:
        return AuthenticateSeller(
            self.database.sellers, self.database.refresh_tokens, self.passwords, self.clock
        )

    @property
    def rotate(self) -> RotateRefreshToken:
        return RotateRefreshToken(self.database.refresh_tokens, self.clock)

    @property
    def revoke(self) -> RevokeRefreshFamily:
        return RevokeRefreshFamily(self.database.refresh_tokens, self.clock)

    async def login(self) -> IssuedRefreshToken:
        grant = await self.authenticate(EMAIL, PASSWORD)
        assert grant.refresh_token is not None
        return grant.refresh_token


@pytest.fixture
async def world() -> World:
    database, clock, passwords = InMemoryDatabase(), FakeClock(), FakePasswordHasher()
    seller = await CreateSeller(database.sellers, passwords, clock)(
        email=EMAIL, slug="demo-seller", display_name="Demo", password=PASSWORD
    )
    passwords.verified.clear()
    return World(database, clock, passwords, seller)


def _refused(reason: RefreshRefusal) -> pytest.RaisesExc[RefreshRefused]:
    return pytest.raises(RefreshRefused, match=f"refresh token refused: {reason}")


# --- Tokens ----------------------------------------------------------------------------


def test_a_refresh_token_is_256_random_bits_in_base64url() -> None:
    tokens = {new_refresh_token() for _ in range(100)}

    assert len(tokens) == 100
    assert {len(token) for token in tokens} == {REFRESH_TOKEN_LENGTH}
    assert all(read_refresh_token(token) == hash_refresh_token(token) for token in tokens)
    assert {len(hash_refresh_token(token)) for token in tokens} == {32}


@pytest.mark.parametrize(
    "text",
    ["", "x" * (REFRESH_TOKEN_LENGTH - 1), "x" * (REFRESH_TOKEN_LENGTH + 1), "+" * 43, "é" * 43],
)
def test_text_that_cannot_be_a_refresh_token_is_refused_before_any_lookup(text: str) -> None:
    assert read_refresh_token(text) is None


# --- Login -----------------------------------------------------------------------------


async def test_a_login_starts_a_family_of_thirty_days(world: World) -> None:
    grant = await world.authenticate(EMAIL, PASSWORD)

    assert grant.seller_id == world.seller.id
    assert grant.refresh_token is not None
    assert grant.refresh_token.expires_at == world.clock.now() + REFRESH_FAMILY_LIFETIME
    stored = await world.database.refresh_tokens.find(hash_refresh_token(grant.refresh_token.value))
    assert stored is not None
    assert stored.seller_id == world.seller.id
    assert stored.is_active(world.clock.now())
    assert world.passwords.verified == [world.seller.password_hash]


async def test_the_email_of_a_login_ignores_case(world: World) -> None:
    grant = await world.authenticate("SELLER@Example.Test", PASSWORD)

    assert grant.seller_id == world.seller.id


async def test_each_login_starts_its_own_family(world: World) -> None:
    first, second = await world.login(), await world.login()

    first_row = await world.database.refresh_tokens.find(hash_refresh_token(first.value))
    second_row = await world.database.refresh_tokens.find(hash_refresh_token(second.value))
    assert first_row is not None
    assert second_row is not None
    assert first_row.family_id != second_row.family_id


async def test_an_unknown_email_and_a_wrong_password_take_the_same_path(world: World) -> None:
    with pytest.raises(InvalidCredentials) as unknown:
        await world.authenticate("nobody@example.test", PASSWORD)
    with pytest.raises(InvalidCredentials) as wrong:
        await world.authenticate(EMAIL, "not the right password")

    # One password check each, the unknown e-mail against the dummy hash, and
    # the same error: nothing tells the two apart.
    assert world.passwords.verified == [FAKE_DUMMY_HASH, world.seller.password_hash]
    assert str(unknown.value) == str(wrong.value) == "invalid e-mail or password"
    assert world.database.tables.refresh_tokens == {}


async def test_a_locked_account_cannot_log_in(world: World) -> None:
    locked = await world.database.sellers.add(
        email="locked@example.test",
        slug="locked",
        display_name="Locked",
        password_hash="!",
        now=world.clock.now(),
    )

    with pytest.raises(InvalidCredentials):
        await world.authenticate(locked.email, "!")
    assert world.passwords.verified == [FAKE_DUMMY_HASH]


def test_every_refusal_is_an_authentication_failure() -> None:
    assert issubclass(InvalidCredentials, AuthenticationFailed)
    assert issubclass(RefreshRefused, AuthenticationFailed)


# --- Rotation --------------------------------------------------------------------------


async def test_a_refresh_rotates_the_token_within_its_family(world: World) -> None:
    token = await world.login()
    world.clock.advance(60)

    grant = await world.rotate(token.value)

    assert grant.seller_id == world.seller.id
    assert grant.refresh_token is not None
    assert grant.refresh_token.value != token.value
    # The successor keeps the family's expiry: a family lives 30 days from its login.
    assert grant.refresh_token.expires_at == token.expires_at
    used = await world.database.refresh_tokens.find(hash_refresh_token(token.value))
    successor = await world.database.refresh_tokens.find(
        hash_refresh_token(grant.refresh_token.value)
    )
    assert used is not None
    assert successor is not None
    assert (used.used_at, used.replaced_by_id) == (world.clock.now(), successor.id)
    assert successor.family_id == used.family_id
    assert successor.is_active(world.clock.now())


async def test_rotations_chain_until_the_family_expires(world: World) -> None:
    token = await world.login()

    for _ in range(5):
        world.clock.advance(timedelta(days=5))
        grant = await world.rotate(token.value)
        assert grant.refresh_token is not None
        token = grant.refresh_token

    world.clock.advance(timedelta(days=5))
    with _refused(RefreshRefusal.EXPIRED):
        await world.rotate(token.value)


@pytest.mark.parametrize("elapsed", [timedelta(0), timedelta(seconds=29.999)])
async def test_a_token_reused_inside_the_grace_gets_an_access_token_only(
    world: World, elapsed: timedelta
) -> None:
    token = await world.login()
    first = await world.rotate(token.value)
    world.clock.advance(elapsed)

    second = await world.rotate(token.value)

    assert second == SellerGrant(world.seller.id, None)
    assert first.refresh_token is not None
    successor = await world.database.refresh_tokens.find(
        hash_refresh_token(first.refresh_token.value)
    )
    assert successor is not None
    assert successor.is_active(world.clock.now())
    assert len(world.database.tables.refresh_tokens) == 2


async def test_two_concurrent_refreshes_give_one_rotation_and_one_access_only_grant(
    world: World,
) -> None:
    token = await world.login()

    grants = await asyncio.gather(world.rotate(token.value), world.rotate(token.value))

    assert sorted(grant.refresh_token is None for grant in grants) == [False, True]
    assert {grant.seller_id for grant in grants} == {world.seller.id}


async def test_a_grace_reuse_holds_after_the_successor_rotated_in_turn(world: World) -> None:
    token = await world.login()
    first = await world.rotate(token.value)
    assert first.refresh_token is not None
    await world.rotate(first.refresh_token.value)
    world.clock.advance(10)

    # Its successor is used, not revoked: two tabs racing twice in a row.
    assert await world.rotate(token.value) == SellerGrant(world.seller.id, None)


@pytest.mark.parametrize("elapsed", [REUSE_GRACE, timedelta(minutes=5)])
async def test_a_token_reused_after_the_grace_revokes_its_family(
    world: World, elapsed: timedelta
) -> None:
    token = await world.login()
    other = await world.login()
    first = await world.rotate(token.value)
    assert first.refresh_token is not None
    world.clock.advance(elapsed)

    with _refused(RefreshRefusal.REUSED) as excinfo:
        await world.rotate(token.value)

    used = await world.database.refresh_tokens.find(hash_refresh_token(token.value))
    assert used is not None
    assert (excinfo.value.seller_id, excinfo.value.family_id) == (world.seller.id, used.family_id)
    family = [
        row
        for row in world.database.tables.refresh_tokens.values()
        if row.family_id == used.family_id
    ]
    assert len(family) == 2
    assert {row.revoked_at for row in family} == {world.clock.now()}
    # The successor held by the seller dies with the family; another login lives on.
    with _refused(RefreshRefusal.REVOKED):
        await world.rotate(first.refresh_token.value)
    assert (await world.rotate(other.value)).refresh_token is not None


async def test_a_token_of_a_revoked_family_is_refused_even_inside_the_grace(
    world: World,
) -> None:
    token = await world.login()
    first = await world.rotate(token.value)
    assert first.refresh_token is not None
    successor = await world.database.refresh_tokens.find(
        hash_refresh_token(first.refresh_token.value)
    )
    assert successor is not None
    await world.database.refresh_tokens.revoke_family(successor.family_id, now=world.clock.now())
    world.clock.advance(1)

    # The token itself is revoked with its family, so the refusal says so first.
    with _refused(RefreshRefusal.REVOKED):
        await world.rotate(token.value)


async def test_a_used_token_whose_successor_alone_is_revoked_is_a_reuse(world: World) -> None:
    token = await world.login()
    first = await world.rotate(token.value)
    assert first.refresh_token is not None
    successor = await world.database.refresh_tokens.find(
        hash_refresh_token(first.refresh_token.value)
    )
    assert successor is not None
    # Only the successor is revoked, as a revocation racing the rotation may leave it.
    world.database.tables.refresh_tokens[successor.id] = replace(
        successor, revoked_at=world.clock.now()
    )

    with _refused(RefreshRefusal.REUSED):
        await world.rotate(token.value)


async def test_an_expired_refresh_token_is_refused_and_revokes_nothing(world: World) -> None:
    token = await world.login()
    world.clock.advance(REFRESH_FAMILY_LIFETIME)

    with _refused(RefreshRefusal.EXPIRED):
        await world.rotate(token.value)

    assert all(row.revoked_at is None for row in world.database.tables.refresh_tokens.values())


async def test_an_unknown_or_malformed_refresh_token_is_refused(world: World) -> None:
    await world.login()

    with _refused(RefreshRefusal.UNKNOWN):
        await world.rotate(new_refresh_token())
    with _refused(RefreshRefusal.MALFORMED):
        await world.rotate("not-a-token")
    assert len(world.database.tables.refresh_tokens) == 1


# --- Logout ----------------------------------------------------------------------------


async def test_a_logout_revokes_the_whole_family_and_nothing_else(world: World) -> None:
    token = await world.login()
    other = await world.login()
    first = await world.rotate(token.value)
    assert first.refresh_token is not None

    # Any token of the family will do, the used one included.
    assert await world.revoke(token.value) is True

    with _refused(RefreshRefusal.REVOKED):
        await world.rotate(first.refresh_token.value)
    assert (await world.rotate(other.value)).refresh_token is not None


@pytest.mark.parametrize("text", ["", "not-a-token", new_refresh_token()])
async def test_a_logout_without_a_known_token_revokes_nothing(world: World, text: str) -> None:
    token = await world.login()

    assert await world.revoke(text) is False
    assert (await world.rotate(token.value)).refresh_token is not None
