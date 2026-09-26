"""``SellerRepository`` on every backend: the in-memory fake, then PostgreSQL."""

from uuid import UUID

import pytest

from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import SellerConflict
from livecart_core.testing.backends import AuthBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START, LOCKED_ACCOUNT

NOW = FAKE_CLOCK_START
UNKNOWN = UUID(int=404)


async def _add(
    backend: AuthBackend, email: str = "Seller@Example.test", slug: str = "demo-seller"
) -> SellerRecord:
    return await backend.sellers.add(
        email=email, slug=slug, display_name="Demo", password_hash=LOCKED_ACCOUNT, now=NOW
    )


async def test_a_seller_reads_back_as_it_was_added(auth_backend: AuthBackend) -> None:
    seller = await _add(auth_backend)

    assert (seller.email, seller.slug, seller.display_name) == (
        "Seller@Example.test",
        "demo-seller",
        "Demo",
    )
    assert seller.password_hash == LOCKED_ACCOUNT
    assert seller.created_at == NOW
    assert await auth_backend.sellers.get(seller.id) == seller
    assert await auth_backend.sellers.get(UNKNOWN) is None


@pytest.mark.parametrize(
    "typed", ["Seller@Example.test", "seller@example.test", "SELLER@EXAMPLE.TEST"]
)
async def test_a_seller_is_found_by_email_whatever_its_case(
    auth_backend: AuthBackend, typed: str
) -> None:
    seller = await _add(auth_backend)
    await _add(auth_backend, "other@example.test", "other")

    assert await auth_backend.sellers.find_by_email(typed) == seller
    assert await auth_backend.sellers.find_by_email("nobody@example.test") is None


async def test_an_email_is_unique_ignoring_case(auth_backend: AuthBackend) -> None:
    seller = await _add(auth_backend)

    with pytest.raises(SellerConflict) as excinfo:
        await _add(auth_backend, "seller@EXAMPLE.test", "another-slug")

    assert excinfo.value.field == "email"
    assert await auth_backend.sellers.find_by_email("seller@example.test") == seller


async def test_a_slug_is_unique(auth_backend: AuthBackend) -> None:
    await _add(auth_backend)

    with pytest.raises(SellerConflict) as excinfo:
        await _add(auth_backend, "other@example.test", "demo-seller")

    assert excinfo.value.field == "slug"
    assert await auth_backend.sellers.find_by_email("other@example.test") is None


async def test_a_seller_is_found_by_the_slug_of_their_storefront(
    auth_backend: AuthBackend,
) -> None:
    seller = await _add(auth_backend)
    await _add(auth_backend, "other@example.test", "other-shop")

    assert await auth_backend.sellers.find_by_slug("demo-seller") == seller
    assert await auth_backend.sellers.find_by_slug("Demo-Seller") is None
    assert await auth_backend.sellers.find_by_slug("unknown-shop") is None
