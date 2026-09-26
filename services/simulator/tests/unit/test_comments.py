"""The simulated comments, read by the real parser against the seeded catalogs.

Every purchase must be a match of exactly the product, variant and quantity
the writer meant, every noise a rejection and every ambiguous comment an
ambiguity: otherwise the demo would show intents nobody asked for, or none.
"""

import random
from collections import Counter

import pytest

from livecart_core.application.demo_seed import FRIDAY_DROP, LOADTEST, DemoSession
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.parser import Ambiguous, Match, Rejected, parse_comment
from livecart_simulator.comments import (
    DEFAULT_MIX,
    MAX_BUYERS,
    CommentKind,
    CommentWriter,
    SimulatedComment,
    SimulatedProduct,
    buyer_ids,
    catalog_of,
)

SAMPLES = 3_000
MAX_QUANTITY = 10


def _writer(
    session: DemoSession = FRIDAY_DROP, *, seed: int = 7, exclude: tuple[str, ...] = ()
) -> CommentWriter:
    return CommentWriter(
        catalog_of(session),
        buyers=buyer_ids(12),
        rng=random.Random(seed),  # noqa: S311 - reproducible test data
        exclude_codes=exclude,
    )


def _parser_catalog(session: DemoSession) -> Catalog:
    return Catalog(
        tuple(
            CatalogProduct(
                ProductCode(product.code), product.name, tuple(map(Variant, product.variants))
            )
            for product in catalog_of(session)
        )
    )


def _agrees(comment: SimulatedComment, catalog: Catalog) -> bool:
    result = parse_comment(comment.text, catalog, max_quantity=MAX_QUANTITY)
    if comment.kind is CommentKind.PURCHASE:
        return (
            isinstance(result, Match)
            and result.product_code.value == comment.code
            and result.variant.value == comment.variant
            and result.quantity.value == comment.quantity
        )
    if comment.kind is CommentKind.NOISE:
        return isinstance(result, Rejected)
    return isinstance(result, Ambiguous)


@pytest.mark.parametrize("session", [FRIDAY_DROP, LOADTEST], ids=lambda s: s.external_live_id)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_parser_reads_every_comment_as_the_writer_meant(
    session: DemoSession, seed: int
) -> None:
    writer = _writer(session, seed=seed)
    catalog = _parser_catalog(session)

    comments = [writer.write() for _ in range(SAMPLES)]

    disagreements = [comment for comment in comments if not _agrees(comment, catalog)]
    assert disagreements == []


def test_the_mix_is_mostly_purchases_with_some_noise_and_ambiguities() -> None:
    writer = _writer()

    kinds = Counter(writer.write().kind for _ in range(SAMPLES))

    for kind, weight in DEFAULT_MIX.items():
        assert kinds[kind] / SAMPLES == pytest.approx(weight / 100, abs=0.03)


def test_purchases_name_every_product_and_every_variant() -> None:
    writer = _writer()

    bought = {
        (comment.code, comment.variant)
        for comment in (writer.write() for _ in range(SAMPLES))
        if comment.kind is CommentKind.PURCHASE
    }

    rows = {(product.code, product.variant) for product in FRIDAY_DROP.products}
    assert bought == rows


def test_comments_come_from_several_buyers() -> None:
    writer = _writer()

    authors = Counter(writer.write().author for _ in range(SAMPLES))

    assert set(authors) == set(buyer_ids(12))


def test_excluded_codes_are_never_named() -> None:
    writer = _writer(exclude=("b07", " E15 "))

    texts = [writer.write().text.upper() for _ in range(SAMPLES)]

    assert {product.code for product in writer.products} == {"A12", "C3", "D4", "Z99", "F8"}
    assert not [text for text in texts if "B07" in text or "E15" in text]
    # Named nowhere else either: neither by the name of the bag nor of the cap.
    assert not [text for text in texts if "SAC" in text or "CASQUETTE" in text]


def test_a_seed_writes_the_same_comments_again() -> None:
    first, second = _writer(seed=42), _writer(seed=42)

    assert [first.write() for _ in range(50)] == [second.write() for _ in range(50)]


def test_an_unknown_excluded_code_is_refused_with_the_catalog() -> None:
    with pytest.raises(ValueError, match=r"does not have: X9 .*A12, B07"):
        _writer(exclude=("X9",))


def test_excluding_every_code_is_refused() -> None:
    with pytest.raises(ValueError, match="leaves no product"):
        _writer(LOADTEST, exclude=("Z99",))


def test_a_writer_needs_buyers_and_a_mix() -> None:
    products = catalog_of(FRIDAY_DROP)
    rng = random.Random(0)  # noqa: S311 - reproducible test data
    with pytest.raises(ValueError, match="one buyer"):
        CommentWriter(products, buyers=(), rng=rng)
    with pytest.raises(ValueError, match="positive weights"):
        CommentWriter(products, buyers=("b",), rng=rng, mix=dict.fromkeys(CommentKind, 0))


def test_the_catalog_groups_the_seeded_rows_by_code() -> None:
    products = catalog_of(FRIDAY_DROP)

    assert [product.code for product in products] == ["A12", "B07", "C3", "D4", "E15", "Z99", "F8"]
    by_code = {product.code: product for product in products}
    assert by_code["A12"] == SimulatedProduct("A12", "Robe fleurie", ("S", "M", "L"))
    assert by_code["B07"].variants == ()
    # A single variant is implied: buyers never name it.
    assert not by_code["E15"].needs_variant
    assert by_code["D4"].sized
    assert not by_code["C3"].sized


def test_buyer_ids_are_numbered_within_bounds() -> None:
    assert buyer_ids(3) == ("sim-buyer-01", "sim-buyer-02", "sim-buyer-03")
    assert buyer_ids(MAX_BUYERS)[-1] == "sim-buyer-1000"
    for count in (0, MAX_BUYERS + 1):
        with pytest.raises(ValueError, match="buyers"):
            buyer_ids(count)
