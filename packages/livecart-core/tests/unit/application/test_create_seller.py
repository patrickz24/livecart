import pytest

from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.ports import SellerConflict
from livecart_core.domain.errors import InvalidValue
from livecart_core.testing.fakes import FakeClock, FakePasswordHasher, InMemoryDatabase

PASSWORD = "correct horse battery staple"


def _create(database: InMemoryDatabase, passwords: FakePasswordHasher) -> CreateSeller:
    return CreateSeller(database.sellers, passwords, FakeClock())


async def test_a_seller_is_stored_with_checked_fields_and_a_hashed_password() -> None:
    database, passwords, clock = InMemoryDatabase(), FakePasswordHasher(), FakeClock()

    seller = await CreateSeller(database.sellers, passwords, clock)(
        email="  Demo@Example.test ",
        slug="demo-seller",
        display_name=" LiveCart Demo ",
        password=PASSWORD,
    )

    assert (seller.email, seller.slug, seller.display_name) == (
        "Demo@Example.test",
        "demo-seller",
        "LiveCart Demo",
    )
    assert seller.password_hash == passwords.hash_of(PASSWORD)
    assert PASSWORD not in seller.password_hash
    assert seller.created_at == clock.now()
    assert await database.sellers.get(seller.id) == seller
    # The hash never shows in a repr, nor then in a log line.
    assert seller.password_hash not in repr(seller)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "not an e-mail"),
        ("slug", "Not A Slug"),
        ("display_name", "   "),
        ("password", "too short"),
    ],
)
async def test_a_refused_field_stores_nothing(field: str, value: str) -> None:
    database = InMemoryDatabase()
    fields = {
        "email": "seller@example.test",
        "slug": "demo-seller",
        "display_name": "Demo",
        "password": PASSWORD,
    }
    fields[field] = value

    with pytest.raises(InvalidValue):
        await _create(database, FakePasswordHasher())(**fields)

    assert database.tables.sellers == {}


async def test_an_email_taken_in_another_case_or_a_taken_slug_is_a_conflict() -> None:
    database, passwords = InMemoryDatabase(), FakePasswordHasher()
    create = _create(database, passwords)
    await create(
        email="seller@example.test", slug="demo-seller", display_name="Demo", password=PASSWORD
    )

    with pytest.raises(SellerConflict) as email:
        await create(
            email="SELLER@example.TEST", slug="other", display_name="Other", password=PASSWORD
        )
    with pytest.raises(SellerConflict) as slug:
        await create(
            email="other@example.test", slug="demo-seller", display_name="Other", password=PASSWORD
        )

    assert (email.value.field, slug.value.field) == ("email", "slug")
    assert len(database.tables.sellers) == 1
