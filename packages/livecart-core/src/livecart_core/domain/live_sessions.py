"""What a seller may still change in a live session and its catalog, as the session goes live.

A comment finds its session by (platform, external_live_id) and its product
by (code, variant): these are the keys of a session. Changing one while the
live runs would send the comments already typed, or about to be, to another
session or another product, so the status decides:

- ``draft``: everything may change;
- ``live``: everything but the keys. The title, a product's name and price
  change, products are added and restocked; an order keeps the unit price it
  was opened at;
- ``ended``: nothing. The session is the record of a finished live.

A session's catalog also follows the parser's shape (``domain.catalog``): a
code has either one row without a variant or only rows with one, and each
(code, variant) appears once. It holds ``MAX_PRODUCTS_PER_SESSION`` rows at
most: the worker loads the whole catalog for each comment it parses, and the
storefront shows it in one answer.
"""

from collections.abc import Collection
from enum import StrEnum
from typing import Final

from livecart_core.domain.codes import Variant
from livecart_core.domain.errors import EditRefused
from livecart_core.domain.state_machines import LiveSessionStatus

MAX_PRODUCTS_PER_SESSION: Final = 200


def check_session_edit(status: LiveSessionStatus, *, keys: bool = False) -> None:
    """Raise ``EditRefused`` unless a session in ``status`` takes a change (of its keys or not)."""
    allowed = status is LiveSessionStatus.DRAFT or (status is LiveSessionStatus.LIVE and not keys)
    if not allowed:
        raise EditRefused(status, keys=keys)


class CatalogConflict(StrEnum):
    """Why a product cannot join a session's catalog, or take a new code or variant in it."""

    # The session sells this code and variant already.
    KEY_TAKEN = "key_taken"
    # The code would hold a row without a variant beside rows with one.
    VARIANT_MIX = "variant_mix"
    # The session holds MAX_PRODUCTS_PER_SESSION products already.
    FULL = "full"


def variant_conflict(same_code: Collection[Variant], variant: Variant) -> CatalogConflict | None:
    """What stops a row of ``variant`` beside the rows of the same code holding ``same_code``."""
    if variant in same_code:
        return CatalogConflict.KEY_TAKEN
    if same_code and (variant.is_empty or any(taken.is_empty for taken in same_code)):
        return CatalogConflict.VARIANT_MIX
    return None
