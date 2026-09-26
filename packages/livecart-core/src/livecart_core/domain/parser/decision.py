"""The decision: one reading in, one result out, in the order of the package docstring."""

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import NO_VARIANT, Variant
from livecart_core.domain.outcomes import AmbiguityReason, RejectionReason
from livecart_core.domain.parser.grammar import Mention, Reading, Reference
from livecart_core.domain.parser.results import (
    Ambiguous,
    Confidence,
    Match,
    ParseResult,
    Rejected,
)
from livecart_core.domain.quantity import parse_quantity


def _evidence(mentions: list[Mention]) -> tuple[str, ...]:
    """The quoted substrings, in text order, each stretch of the comment once."""
    distinct = {(mention.start, mention.end): mention.text for mention in mentions}
    return tuple(distinct[span] for span in sorted(distinct))


def _everything_recognised(reading: Reading) -> list[Mention]:
    return [
        *reading.signals,
        *reading.typo_signals,
        *reading.doubts,
        *(mention for reference in reading.references for mention in reference.mentions),
        *(quantity.mention for quantity in reading.quantities),
        *(variant.mention for variant in reading.variants),
        *reading.variant_words,
        *reading.number_words,
        *(mention for named in reading.named_products for mention in named.mentions),
    ]


def _confidence(reading: Reading, reference: Reference) -> Confidence:
    if reading.signals:
        signal = Confidence.EXACT
    elif reading.typo_signals:
        signal = Confidence.CORRECTED
    else:
        signal = Confidence.CODE_ALONE
    code = Confidence.CORRECTED if reference.corrected else Confidence.EXACT
    return min(signal, code, key=lambda level: level.value)


def decide(reading: Reading, catalog: Catalog, *, max_quantity: int) -> ParseResult:
    """The result of the first rule that applies; the rules are numbered as documented."""
    has_signal = bool(reading.signals or reading.typo_signals)
    # 1. Without a buy signal, only a bare code is an order.
    if not has_signal and (not reading.references or reading.doubts or reading.content):
        return Rejected(RejectionReason.NO_INTENT)
    # 2. The pipeline reserves one product per comment.
    if len(reading.references) >= 2:
        return Rejected(RejectionReason.MULTIPLE_PRODUCTS)
    # 3. A buy signal with nothing to buy.
    if not reading.references and not reading.named_products:
        return Rejected(RejectionReason.NO_INTENT)
    # 4. Out of bounds whatever the product turns out to be, but only for a
    # number that can be nothing but a quantity: a bare "50" may be a price,
    # a date or a size, so it is left to rule 8.
    values = {quantity.value for quantity in reading.quantities}
    in_bounds = {value for value in values if 1 <= value <= max_quantity}
    definite = any(quantity.marked or quantity.value < 1 for quantity in reading.quantities)
    if values and not in_bounds and definite:
        return Rejected(RejectionReason.QUANTITY_OUT_OF_BOUNDS)
    reference = reading.references[0] if reading.references else None
    product = (
        catalog.get(reference.code)
        if reference is not None and reference.code is not None
        else None
    )
    found = list(dict.fromkeys(variant.variant for variant in reading.variants))
    # 5. A size or colour this product does not come in.
    if product is not None and product.has_variants and not found and reading.variant_words:
        return Rejected(RejectionReason.VARIANT_NOT_FOUND)

    evidence = _evidence(_everything_recognised(reading))
    # 6. A product named in words, never by its code.
    if reference is None:
        named = tuple(named.code for named in reading.named_products)
        return Ambiguous(AmbiguityReason.PRODUCT_BY_NAME, named, evidence)
    # 7. A code-shaped token that no catalog code matches, or several do.
    if product is None:
        return Ambiguous(AmbiguityReason.UNKNOWN_CODE, reference.candidates, evidence)
    # 8. Signals that pull two ways, or a bare number too large to be the quantity.
    if (
        reading.doubts
        or len(values) >= 2
        or (values and not in_bounds)
        or reading.number_words
        or len(found) >= 2
    ):
        return Ambiguous(AmbiguityReason.CONFLICTING_SIGNALS, (product.code,), evidence)
    # 9. Which size or colour? A product with a single variant needs no answer.
    variant: Variant = NO_VARIANT
    if found:
        variant = found[0]
    elif len(product.variants) == 1:
        variant = product.variants[0]
    elif product.has_variants:
        return Ambiguous(AmbiguityReason.UNRESOLVED_VARIANT, (product.code,), evidence)

    # 10. The match, quoting what it rests on.
    quantity = parse_quantity(values.pop() if values else 1, max_quantity=max_quantity)
    used = [
        *reading.signals,
        *reading.typo_signals,
        *reference.mentions,
        *(mention.mention for mention in reading.variants if mention.variant == variant),
        *(mention.mention for mention in reading.quantities),
    ]
    return Match(
        product_code=product.code,
        variant=variant,
        quantity=quantity,
        confidence=_confidence(reading, reference),
        evidence=_evidence(used),
    )
