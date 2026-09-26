"""``RefreshTokenRepository`` on every backend: the in-memory fake, then PostgreSQL.

Each scenario runs twice through the ``auth_backend`` fixture; the PostgreSQL
run is marked ``integration``. The race of a revocation with a rotation that
commits meanwhile, which only a real database can interleave, is in
``tests/integration/test_refresh_token_races.py``.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import RefreshTokenRecord
from livecart_core.application.ports import RecordNotFound
from livecart_core.application.seller_auth import hash_refresh_token, new_refresh_token
from livecart_core.testing.backends import LOCKED_ACCOUNT, AuthBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
EXPIRES_AT = NOW + timedelta(days=30)
UNKNOWN = UUID(int=404)


def _hash() -> bytes:
    return hash_refresh_token(new_refresh_token())


async def _seller(backend: AuthBackend, slug: str = "demo-seller") -> UUID:
    seller = await backend.sellers.add(
        email=f"{slug}@example.test",
        slug=slug,
        display_name="Demo",
        password_hash=LOCKED_ACCOUNT,
        now=NOW,
    )
    return seller.id


async def _login(backend: AuthBackend, seller_id: UUID) -> tuple[bytes, RefreshTokenRecord]:
    token_hash = _hash()
    record = await backend.refresh_tokens.issue(
        seller_id, token_hash, now=NOW, expires_at=EXPIRES_AT
    )
    return token_hash, record


async def test_a_login_starts_a_family_with_one_active_token(auth_backend: AuthBackend) -> None:
    seller_id = await _seller(auth_backend)

    token_hash, issued = await _login(auth_backend, seller_id)

    assert (issued.seller_id, issued.expires_at, issued.created_at) == (seller_id, EXPIRES_AT, NOW)
    assert issued.is_active(NOW)
    assert issued.family_id != issued.id
    assert await auth_backend.refresh_tokens.find(token_hash) == issued
    assert await auth_backend.refresh_tokens.get(issued.id) == issued
    assert await auth_backend.refresh_tokens.find(_hash()) is None
    assert await auth_backend.refresh_tokens.get(UNKNOWN) is None


async def test_a_token_of_an_unknown_seller_is_refused(auth_backend: AuthBackend) -> None:
    with pytest.raises(RecordNotFound):
        await auth_backend.refresh_tokens.issue(UNKNOWN, _hash(), now=NOW, expires_at=EXPIRES_AT)


async def test_a_rotation_uses_the_token_and_stores_its_successor(
    auth_backend: AuthBackend,
) -> None:
    seller_id = await _seller(auth_backend)
    token_hash, issued = await _login(auth_backend, seller_id)
    successor_hash = _hash()
    later = NOW + timedelta(minutes=10)

    successor = await auth_backend.refresh_tokens.rotate(token_hash, successor_hash, now=later)

    assert successor is not None
    assert (successor.seller_id, successor.family_id) == (seller_id, issued.family_id)
    # The family's expiry is inherited, not extended.
    assert (successor.expires_at, successor.created_at) == (EXPIRES_AT, later)
    assert successor.is_active(later)
    assert await auth_backend.refresh_tokens.find(successor_hash) == successor
    used = await auth_backend.refresh_tokens.find(token_hash)
    assert used is not None
    assert (used.used_at, used.replaced_by_id, used.revoked_at) == (later, successor.id, None)


async def test_a_used_token_rotates_no_more_and_writes_nothing(auth_backend: AuthBackend) -> None:
    seller_id = await _seller(auth_backend)
    token_hash, _ = await _login(auth_backend, seller_id)
    successor = await auth_backend.refresh_tokens.rotate(token_hash, _hash(), now=NOW)
    before = await auth_backend.refresh_tokens.find(token_hash)
    late_hash = _hash()

    assert await auth_backend.refresh_tokens.rotate(token_hash, late_hash, now=NOW) is None

    assert await auth_backend.refresh_tokens.find(token_hash) == before
    assert await auth_backend.refresh_tokens.find(late_hash) is None
    assert successor is not None
    assert (await auth_backend.refresh_tokens.get(successor.id)) == successor


async def test_concurrent_rotations_of_one_token_have_one_winner(
    auth_backend: AuthBackend,
) -> None:
    seller_id = await _seller(auth_backend)
    token_hash, issued = await _login(auth_backend, seller_id)
    successor_hashes = [_hash() for _ in range(5)]

    results = await asyncio.gather(
        *(
            auth_backend.refresh_tokens.rotate(token_hash, successor_hash, now=NOW)
            for successor_hash in successor_hashes
        )
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = [await auth_backend.refresh_tokens.find(value) for value in successor_hashes]
    assert [record for record in stored if record is not None] == winners
    used = await auth_backend.refresh_tokens.get(issued.id)
    assert used is not None
    assert used.replaced_by_id == winners[0].id


async def test_an_expired_token_does_not_rotate(auth_backend: AuthBackend) -> None:
    seller_id = await _seller(auth_backend)
    token_hash, issued = await _login(auth_backend, seller_id)

    assert await auth_backend.refresh_tokens.rotate(token_hash, _hash(), now=EXPIRES_AT) is None
    assert await auth_backend.refresh_tokens.get(issued.id) == issued


async def test_an_unknown_token_does_not_rotate(auth_backend: AuthBackend) -> None:
    await _login(auth_backend, await _seller(auth_backend))

    assert await auth_backend.refresh_tokens.rotate(_hash(), _hash(), now=NOW) is None


async def test_revoking_a_family_revokes_each_of_its_tokens_once(
    auth_backend: AuthBackend,
) -> None:
    seller_id = await _seller(auth_backend)
    token_hash, issued = await _login(auth_backend, seller_id)
    successor = await auth_backend.refresh_tokens.rotate(token_hash, _hash(), now=NOW)
    other_hash, other = await _login(auth_backend, seller_id)
    later = NOW + timedelta(seconds=45)

    revoked = await auth_backend.refresh_tokens.revoke_family(issued.family_id, now=later)

    assert revoked == 2
    assert successor is not None
    for token_id in (issued.id, successor.id):
        row = await auth_backend.refresh_tokens.get(token_id)
        assert row is not None
        assert row.revoked_at == later
    # A revoked token rotates no more, a second revocation changes nothing,
    # and another family of the same seller is untouched.
    assert await auth_backend.refresh_tokens.rotate(token_hash, _hash(), now=later) is None
    again = later + timedelta(seconds=1)
    assert await auth_backend.refresh_tokens.revoke_family(issued.family_id, now=again) == 0
    first = await auth_backend.refresh_tokens.get(issued.id)
    assert first is not None
    assert first.revoked_at == later
    assert await auth_backend.refresh_tokens.find(other_hash) == other
    assert await auth_backend.refresh_tokens.revoke_family(UNKNOWN, now=later) == 0
