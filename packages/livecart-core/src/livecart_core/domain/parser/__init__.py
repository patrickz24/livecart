"""The deterministic purchase-intent parser: ``parse_comment`` is its one entry point.

It reads one live comment against the catalog of the session and returns a
``Match`` (one product, one variant, a quantity), an ``Ambiguous`` result (the
only kind a later LLM fallback sees) or a ``Rejected`` one. It is pure: no
I/O, no clock, no settings; the bound on the quantity arrives as an argument.
The same comment and catalog always give the same result.

Pipeline, one module per step:

1. ``normalization``: NFKC, case folding and accent stripping on a copy of
   the text, keeping a map to the original characters for the evidence;
   keycap digits read as digits, the multiplication signs as ``x``, the cart
   emoji as a buy signal, meaningless punctuation as spaces;
2. ``tokens``: words, numbers, glued quantities, catalog codes (as typed, or
   through exactly one O/0 or I/1 swap reading) and code-shaped unknown tokens;
3. ``grammar``: one role per token: product reference, variant, buy signal
   (a verb, possibly with one typo), doubt (question, negation), quantity,
   filler or content;
4. ``decision``: the first rule that applies, below.

Decision order. Rejections come first when they hold whatever an LLM could
read into the comment, so that ambiguities stay rare:

1. ``no_intent``: no buy signal, and the comment is not a bare code: there is
   no code (known or not), or a question or negation ("C3 dispo ?"), or a
   content word ("trop beau le C3"). A bare code may carry a quantity, a
   size or colour, fillers ("svp", "pour moi") and words of the product's
   own name;
2. ``multiple_products``: two or more distinct codes, known or not;
3. ``no_intent``: a buy signal, but neither a code nor a catalog product name;
4. ``quantity_out_of_bounds``: every quantity given is below 1 or above
   ``max_quantity``;
5. ``variant_not_found``: the product has variants, none is named, and a
   size or colour word (or the word after "taille", "size"...) is given;
6. ``product_by_name`` (ambiguous): no code, but a word of a catalog product
   name, next to a buy signal;
7. ``unknown_code`` (ambiguous): the code matches no catalog code, even
   through a swap, or several catalog codes through swaps;
8. ``conflicting_signals`` (ambiguous): a buy signal with a question or a
   negation ("je prends C3 ?"), two different quantities, a number written in
   words, or two different variants;
9. ``unresolved_variant`` (ambiguous): the product has several variants and
   none is named (a product with a single variant takes it);
10. ``Match``, with quantity 1 unless one is given.

Confidence of a match, from the weakest evidence it relied on:
``Confidence.EXACT`` (1.00) for a buy signal and a code as typed,
``Confidence.CODE_ALONE`` (0.90) for a code without a buy signal, and
``Confidence.CORRECTED`` (0.80) as soon as a verb typo or a code swap was
needed. The evidence is the list of substrings of the original comment that
the result rests on, in text order.

Known limits, by design: quantities are read in digits and keycaps only, a
number written in words is left to the fallback; names are only looked at
when no code is given, and a size or colour alone ("je prends la bleue")
never identifies a product; comments about several products are rejected,
as the MVP reserves one product per comment.
"""

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.parser.decision import decide
from livecart_core.domain.parser.grammar import read_comment
from livecart_core.domain.parser.normalization import normalize_comment
from livecart_core.domain.parser.results import (
    Ambiguous,
    Confidence,
    Match,
    ParseResult,
    Rejected,
)
from livecart_core.domain.parser.tokens import tokenize
from livecart_core.domain.quantity import QUANTITY_CEILING

__all__ = ["Ambiguous", "Confidence", "Match", "ParseResult", "Rejected", "parse_comment"]


def parse_comment(text: str, catalog: Catalog, *, max_quantity: int) -> ParseResult:
    """Read one comment against the session catalog; see the module docstring."""
    if not 1 <= max_quantity <= QUANTITY_CEILING:
        # A caller bug, not bad input: the settings validator keeps the bound in range.
        raise ValueError(f"max_quantity must be from 1 to {QUANTITY_CEILING}, not {max_quantity}")
    folded = normalize_comment(text)
    reading = read_comment(text, tokenize(folded, catalog), catalog)
    return decide(reading, catalog, max_quantity=max_quantity)
