"""The rules of a seller account: its e-mail, storefront slug, display name and password.

There is no public sign-up: an operator creates each account
(``livecart-create-seller``, the demo seed), and these rules check what they
type before anything is hashed or stored. Each check returns the value to
store, or raises ``InvalidValue`` with a message that never repeats a password.

- The e-mail signs the seller in and is unique whatever its case
  (``uq_sellers_email`` indexes ``lower(email)``). It is limited to printable
  ASCII so that PostgreSQL's ``lower()`` and Python's ``str.lower()`` agree on
  what "the same e-mail" means; beyond that, only its shape is checked, since
  only the operator who types it can prove it is delivered.
- The slug is the storefront's URL segment: lower-case letters and digits in
  words joined by single hyphens, the shape of ``ck_sellers_slug``.
- The password is 12 to 128 characters, of any kind: length is what resists
  guessing, and the upper bound keeps one login from costing more than
  another. No composition rule, as NIST SP 800-63B recommends. The bounds apply
  when a password is chosen, not when one is checked at login.
"""

import re
from typing import Final

from livecart_core.domain.errors import InvalidValue

# The longest address SMTP delivers (RFC 5321 path limit): the width of sellers.email.
MAX_EMAIL_LENGTH: Final = 254
# The width of sellers.slug.
MAX_SLUG_LENGTH: Final = 64
# The width of sellers.display_name.
MAX_DISPLAY_NAME_LENGTH: Final = 100
MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128
# Kept in step with ck_sellers_slug, which anchors it with ^ and $.
SLUG_PATTERN: Final = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# One "@" with something on each side, and no whitespace anywhere.
_EMAIL_SHAPE: Final = re.compile(r"[^@\s]+@[^@\s]+")


def check_email(email: str) -> str:
    """The e-mail to store: stripped, printable ASCII, ``local@domain``, 254 characters at most."""
    value = email.strip()
    if not value or len(value) > MAX_EMAIL_LENGTH:
        raise InvalidValue(f"an e-mail has 1 to {MAX_EMAIL_LENGTH} characters")
    if not (value.isascii() and value.isprintable()) or not _EMAIL_SHAPE.fullmatch(value):
        raise InvalidValue(f"{value!r} is not an e-mail address in printable ASCII")
    return value


def check_slug(slug: str) -> str:
    """The storefront slug as typed: lower-case words joined by hyphens, 64 characters at most."""
    if len(slug) > MAX_SLUG_LENGTH or not SLUG_PATTERN.fullmatch(slug):
        raise InvalidValue(
            f"{slug!r} is not a slug: lower-case letters and digits in words joined by single "
            f"hyphens, {MAX_SLUG_LENGTH} characters at most"
        )
    return slug


def check_display_name(display_name: str) -> str:
    """The name the storefront shows: stripped, 1 to 100 printable characters."""
    value = display_name.strip()
    if not 1 <= len(value) <= MAX_DISPLAY_NAME_LENGTH or not value.isprintable():
        raise InvalidValue(
            f"a display name has 1 to {MAX_DISPLAY_NAME_LENGTH} printable characters"
        )
    return value


def check_password(password: str) -> str:
    """A new password, unchanged: 12 to 128 characters. The message only states the bounds."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidValue(
            f"a password has {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters"
        )
    return password
