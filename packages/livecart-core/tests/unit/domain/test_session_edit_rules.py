import pytest

from livecart_core.domain.codes import Variant
from livecart_core.domain.errors import DomainError, EditRefused
from livecart_core.domain.live_sessions import (
    MAX_PRODUCTS_PER_SESSION,
    CatalogConflict,
    check_session_edit,
    variant_conflict,
)
from livecart_core.domain.state_machines import LiveSessionStatus

DRAFT, LIVE, ENDED = LiveSessionStatus.DRAFT, LiveSessionStatus.LIVE, LiveSessionStatus.ENDED


@pytest.mark.parametrize(
    ("status", "keys", "allowed"),
    [
        (DRAFT, False, True),
        (DRAFT, True, True),
        (LIVE, False, True),
        (LIVE, True, False),
        (ENDED, False, False),
        (ENDED, True, False),
    ],
)
def test_a_session_takes_changes_by_status(
    status: LiveSessionStatus, keys: bool, allowed: bool
) -> None:
    if allowed:
        check_session_edit(status, keys=keys)
        return
    with pytest.raises(EditRefused) as refused:
        check_session_edit(status, keys=keys)
    assert (refused.value.status, refused.value.keys) == (status, keys)


def test_every_status_is_covered_by_the_rule() -> None:
    assert set(LiveSessionStatus) == {DRAFT, LIVE, ENDED}


def test_a_refused_edit_says_what_the_status_still_forbids() -> None:
    keys = EditRefused(LIVE, keys=True)
    anything = EditRefused(ENDED, keys=False)

    assert isinstance(keys, DomainError)
    assert str(keys) == (
        "the session is live: it can no longer change its platform, live id and product codes"
    )
    assert str(anything) == "the session is ended: it can no longer change anything"


@pytest.mark.parametrize(
    ("same_code", "variant", "conflict"),
    [
        ((), "", None),
        ((), "M", None),
        (("S",), "M", None),
        (("S", "M"), "L", None),
        (("M",), "M", CatalogConflict.KEY_TAKEN),
        (("",), "", CatalogConflict.KEY_TAKEN),
        (("",), "M", CatalogConflict.VARIANT_MIX),
        (("S", "M"), "", CatalogConflict.VARIANT_MIX),
    ],
)
def test_a_code_holds_one_row_without_variant_or_only_rows_with_one(
    same_code: tuple[str, ...], variant: str, conflict: CatalogConflict | None
) -> None:
    taken = [Variant(label) for label in same_code]

    assert variant_conflict(taken, Variant(variant)) is conflict


def test_a_catalog_is_bounded_well_above_a_live_show() -> None:
    # A session sells tens of products; the bound keeps the per-comment catalog load small.
    assert MAX_PRODUCTS_PER_SESSION == 200
