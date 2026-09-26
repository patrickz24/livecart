from livecart_core.testing.fakes import FAKE_DUMMY_HASH, FakePasswordHasher

PASSWORD = "correct horse battery staple"


async def test_a_fake_hash_verifies_its_password_only() -> None:
    passwords = FakePasswordHasher()
    stored = await passwords.hash(PASSWORD)

    assert stored == FakePasswordHasher.hash_of(PASSWORD)
    assert PASSWORD not in stored
    assert await passwords.verify(stored, PASSWORD) is True
    assert await passwords.verify(stored, PASSWORD.upper()) is False
    assert passwords.verified == [stored, stored]


async def test_an_unknown_or_locked_account_is_checked_against_the_dummy() -> None:
    passwords = FakePasswordHasher()

    assert await passwords.verify(None, PASSWORD) is False
    assert await passwords.verify("!", "!") is False
    assert passwords.verified == [FAKE_DUMMY_HASH, FAKE_DUMMY_HASH]
