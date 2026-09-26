"""``CreateSeller``: open a seller account, the only way one comes to exist.

There is no public sign-up. An operator runs ``livecart-create-seller``, and
the demo seed creates its sellers the same way. The domain checks every field
before the password is hashed, so a refused value costs no argon2 run, and
the repository refuses an e-mail or a slug that another seller has.
"""

from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import Clock, PasswordHasher, SellerRepository
from livecart_core.domain.sellers import (
    check_display_name,
    check_email,
    check_password,
    check_slug,
)


class CreateSeller:
    """Check the account's fields, hash the password, store the seller.

    Raises ``InvalidValue`` (a ``ValueError``) for a field the domain refuses,
    and ``SellerConflict`` when the e-mail, whatever its case, or the slug is
    taken.
    """

    def __init__(self, sellers: SellerRepository, passwords: PasswordHasher, clock: Clock) -> None:
        self._sellers = sellers
        self._passwords = passwords
        self._clock = clock

    async def __call__(
        self, *, email: str, slug: str, display_name: str, password: str
    ) -> SellerRecord:
        checked_email = check_email(email)
        checked_slug = check_slug(slug)
        checked_name = check_display_name(display_name)
        password_hash = await self._passwords.hash(check_password(password))
        return await self._sellers.add(
            email=checked_email,
            slug=checked_slug,
            display_name=checked_name,
            password_hash=password_hash,
            now=self._clock.now(),
        )
