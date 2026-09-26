"""What the simulated audience types: purchases mostly, some noise, a few ambiguous comments.

The comments follow the shapes of the parser's corpus
(``livecart_core.testing.corpus``), in French and in English, against the
catalog of a seeded demo session (``livecart_core.application.demo_seed``),
so that the simulator names the very products ``docker compose up`` seeds:

- **purchases** (75 % by default): a buy verb or a cart, a code, the variant
  when the product has several, sometimes a quantity (``x2``). The parser
  reads each one as a match of exactly that product, variant and quantity:
  the unit tests parse every shape against the catalog;
- **noise** (15 %): greetings, praise and questions, which the parser
  rejects as ``no_intent``;
- **ambiguous** (10 %): a code without its variant, a buy verb with a
  question, a number in words, a product named rather than coded, an
  unknown code. The parser leaves them to the LLM fallback (milestone 4c),
  which turns them into ``ambiguous_unresolved`` rejections without a key.

Each comment comes from one of several fake buyers, so that the rule of one
pending order per buyer and product (``uq_orders_pending_buyer_product``)
shows as ``already_reserved`` intents now and then. Excluded codes are never
named, which keeps their stock for a manual test during a run.
"""

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from livecart_core.application.demo_seed import DemoSession

MAX_BUYERS: Final = 1_000
DEFAULT_BUYERS: Final = 12


class CommentKind(StrEnum):
    """What the simulator meant a comment to be, which the parser must agree with."""

    PURCHASE = "purchase"
    NOISE = "noise"
    AMBIGUOUS = "ambiguous"


# Out of 100 comments, by default.
DEFAULT_MIX: Final[Mapping[CommentKind, int]] = {
    CommentKind.PURCHASE: 75,
    CommentKind.NOISE: 15,
    CommentKind.AMBIGUOUS: 10,
}
# Most buyers take one; some take two or three.
QUANTITY_WEIGHTS: Final[Mapping[int, int]] = {1: 70, 2: 20, 3: 10}


@dataclass(frozen=True, slots=True)
class SimulatedProduct:
    """One code of the catalog and its variant labels, as the seller declared them."""

    code: str
    name: str
    variants: tuple[str, ...] = ()

    @property
    def needs_variant(self) -> bool:
        """Whether a buyer must name a variant: a single one is implied."""
        return len(self.variants) > 1

    @property
    def sized(self) -> bool:
        """Sizes (S, M, L or 36, 38...), as opposed to colours."""
        return all(len(label) <= 3 for label in self.variants)


@dataclass(frozen=True, slots=True)
class SimulatedComment:
    """A comment to post, who posts it, and what the parser should make of it.

    ``code``, ``variant`` and ``quantity`` are the intent of a purchase, and
    empty for the other kinds.
    """

    kind: CommentKind
    text: str
    author: str
    code: str = ""
    variant: str = ""
    quantity: int = 0


def catalog_of(session: DemoSession) -> tuple[SimulatedProduct, ...]:
    """The seeded session's products grouped by code, in the seed's order."""
    variants: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for product in session.products:
        names.setdefault(product.code, product.name)
        labels = variants.setdefault(product.code, [])
        if product.variant:
            labels.append(product.variant)
    return tuple(SimulatedProduct(code, names[code], tuple(variants[code])) for code in names)


def buyer_ids(count: int) -> tuple[str, ...]:
    """The platform user ids of the fake buyers: ``sim-buyer-01`` and so on."""
    if not 1 <= count <= MAX_BUYERS:
        raise ValueError(f"the audience has 1 to {MAX_BUYERS} buyers, not {count}")
    width = max(2, len(str(count)))
    return tuple(f"sim-buyer-{number:0{width}d}" for number in range(1, count + 1))


# --- Purchases -------------------------------------------------------------------------
# {item} is the code and the variant, {qty} the quantity (empty for one).

PURCHASES_FR: Final = (
    "je prends {item}{qty}",
    "je veux {item}{qty} svp",
    "j'achète {item}{qty}",
    "je réserve {item}{qty}",
    "je prends {item}{qty} merci 🙏",
    "{item}{qty} pour moi",
)
PURCHASES_EN: Final = (
    "SOLD {item}{qty}",
    "sold {item}{qty}",
    "i'll take {item}{qty}",
    "I want {item}{qty} please",
    "{item}{qty} 🛒",
)


def _variant_phrase(product: SimulatedProduct, label: str, rng: random.Random, *, fr: bool) -> str:
    """How a buyer names the variant: its label, or "taille M", "size 38", "en rouge"."""
    if not product.needs_variant:
        return ""
    if product.sized:
        lead = rng.choice(("", "taille " if fr else "size "))
        return f" {lead}{label}"
    colour = label.lower()
    return f" {rng.choice(('', 'en ')) if fr else ''}{colour}"


def _quantity_phrase(quantity: int) -> str:
    return "" if quantity == 1 else f" x{quantity}"


# --- Noise -----------------------------------------------------------------------------
# {code} is any product of the catalog: a comment about it that orders nothing.

NOISE: Final = (
    "bonjour tout le monde 👋",
    "trop beau",
    "trop belle cette robe 😍",
    "j'adore la robe",
    "prix ?",
    "c'est combien la livraison ?",
    "vous livrez en Belgique ?",
    "hello from Lyon ☀️",
    "first time here",
    "love this live ❤️",
    "🔥🔥🔥",
    "{code} dispo ?",
    "{code} c'est combien ?",
    "trop beau le {code}",
    "@marie regarde le {code}",
)

# --- Ambiguous comments ----------------------------------------------------------------

# A product with several variants, named without one, or with two.
AMBIGUOUS_VARIANT: Final = (
    "je prends {code}",
    "slod {code}",
    "{code}",
    "je prends {code} {first} ou {second}",
)
# Any product: a question, a negation or a number in words beside the purchase.
AMBIGUOUS_ANY: Final = (
    "je prends {code} ?",
    "je prends deux {code}",
    "je veux {code} mais pas tout de suite",
)
# A product by its name instead of its code.
AMBIGUOUS_NAME: Final = (
    "je prends {name}",
    "i'll take the {name}",
)
# Codes of no product of the demo catalog: a typo, or a product of another live.
AMBIGUOUS_UNKNOWN: Final = (
    "je prends A13",
    "sold C4 M",
    "je prends Q7 x2",
)


class CommentWriter:
    """Writes the simulated comments of one live session; see the module docstring.

    ``rng`` decides everything, so a seeded one writes the same comments again.
    """

    def __init__(
        self,
        products: Sequence[SimulatedProduct],
        *,
        buyers: Sequence[str],
        rng: random.Random,
        exclude_codes: Iterable[str] = (),
        mix: Mapping[CommentKind, int] = DEFAULT_MIX,
    ) -> None:
        excluded = {code.strip().upper() for code in exclude_codes if code.strip()}
        known = {product.code for product in products}
        if unknown := sorted(excluded - known):
            raise ValueError(
                f"--exclude-codes names codes the catalog does not have: {', '.join(unknown)} "
                f"(its codes: {', '.join(sorted(known))})"
            )
        self._products = tuple(product for product in products if product.code not in excluded)
        if not self._products:
            raise ValueError("--exclude-codes leaves no product to comment on")
        if not buyers:
            raise ValueError("the audience needs one buyer at least")
        if sum(mix.values()) <= 0 or min(mix.values()) < 0:
            raise ValueError("the mix of comment kinds needs positive weights")
        self._buyers = tuple(buyers)
        self._rng = rng
        self._kinds = tuple(mix)
        self._weights = tuple(mix[kind] for kind in self._kinds)
        self._writers: Mapping[CommentKind, Callable[[str], SimulatedComment]] = {
            CommentKind.PURCHASE: self._purchase,
            CommentKind.NOISE: self._noise,
            CommentKind.AMBIGUOUS: self._ambiguous,
        }

    @property
    def products(self) -> tuple[SimulatedProduct, ...]:
        """The products the comments may name, the excluded ones left out."""
        return self._products

    def write(self) -> SimulatedComment:
        """The next comment, from a buyer drawn at random."""
        kind = self._rng.choices(self._kinds, weights=self._weights)[0]
        return self._writers[kind](self._rng.choice(self._buyers))

    def _purchase(self, author: str) -> SimulatedComment:
        rng = self._rng
        product = rng.choice(self._products)
        label = rng.choice(product.variants) if product.variants else ""
        quantity = rng.choices(tuple(QUANTITY_WEIGHTS), weights=tuple(QUANTITY_WEIGHTS.values()))[0]
        fr = rng.random() < 0.6
        template = rng.choice(PURCHASES_FR if fr else PURCHASES_EN)
        item = product.code + _variant_phrase(product, label, rng, fr=fr)
        text = template.format(item=item, qty=_quantity_phrase(quantity))
        return SimulatedComment(CommentKind.PURCHASE, text, author, product.code, label, quantity)

    def _noise(self, author: str) -> SimulatedComment:
        template = self._rng.choice(NOISE)
        text = template.format(code=self._rng.choice(self._products).code)
        return SimulatedComment(CommentKind.NOISE, text, author)

    def _ambiguous(self, author: str) -> SimulatedComment:
        rng = self._rng
        with_variants = [product for product in self._products if product.needs_variant]
        shapes: list[Callable[[], str]] = [
            lambda: rng.choice(AMBIGUOUS_ANY).format(code=rng.choice(self._products).code),
            lambda: rng.choice(AMBIGUOUS_NAME).format(name=rng.choice(self._products).name.lower()),
            lambda: rng.choice(AMBIGUOUS_UNKNOWN),
        ]
        if with_variants:
            shapes.append(lambda: self._without_variant(rng.choice(with_variants)))
        return SimulatedComment(CommentKind.AMBIGUOUS, rng.choice(shapes)(), author)

    def _without_variant(self, product: SimulatedProduct) -> str:
        first, second = self._rng.sample(product.variants, 2)
        template = self._rng.choice(AMBIGUOUS_VARIANT)
        return template.format(code=product.code, first=first.lower(), second=second.lower())
