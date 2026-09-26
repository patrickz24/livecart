# ADR 0006: Deterministic parser, then an LLM fallback: closed reasons, structured outputs, budget, bounded slot, model choice

## Status

Accepted, 2026-09-26. Milestone 2 implemented the parser, its corpus and the port of the fallback; the Claude adapter, its structured outputs, budget, bounded slot and the choice of model arrive in milestone 4c, which completes this record.

## Context

Buyers write their orders in the comments of a live stream, in French or English, with typos, emojis, glued codes and sizes: "SOLD A12 M x2", "slod C3", "Ｃ３ je prends", "je prends A12 M en 2". Many comments are no order at all ("C3 dispo ?", "trop beau"). A spike brings many comments per second, each one read by the worker. A language model reads such text well, but each call costs money and seconds, a provider can be slow or down, and a comment is untrusted input that may try to steer the model. The reasons for a rejection are a closed list, shared by a database `CHECK`, the metric labels and the dashboard.

## Decision

### A deterministic parser decides first

- `parse_comment(text, catalog, *, max_quantity)` (`livecart_core.domain.parser`) is pure: no I/O, no clock, no settings, and the same comment and catalog always give the same result. It runs in four steps, one module each: normalisation (NFKC, case folding, accents, keycap digits, multiplication signs, the cart emoji, decimal separators and minus signs, mentions and elisions), tokens (catalog codes as typed or through exactly one O/0 or I/1 swap reading, glued quantities, code-shaped unknown tokens), grammar (one role per token from a closed French and English lexicon, with one typo tolerated on a buy verb under three guards: four letters at least, the right first letter, and not a known word), and the decision (the first rule that applies).
- It returns a `Match` (one product, one variant, a quantity from 1 to `max_quantity`, a confidence of 1.00, 0.90 or 0.80, and the substrings of the comment it rests on), an `Ambiguous` result (a reason, the candidate codes, the evidence) or a `Rejected` one.
- Rejections come first, and only when they hold whatever a model could read into the comment: `no_intent`, `multiple_products`, `quantity_out_of_bounds` (a number marked as a quantity, "x50" or "50 pcs", or below 1) and `variant_not_found`. The ambiguities are `unknown_code`, `product_by_name`, `unresolved_variant` and `conflicting_signals`, and they are the only results the fallback sees.
- Between a match and an ambiguity, the parser picks the ambiguity: a wrong match reserves stock at full confidence, while an ambiguity costs one fallback call or, without a fallback, an `ambiguous_unresolved` rejection. A buy verb with a question, a negation or a past-tense marker ("j'ai déjà commandé"), a bare number above the bound ("le 12"), a number in words or with look-alike letters ("1O") and a colour that a qualifier may lengthen ("bleu foncé" beside "Bleu nuit") are therefore conflicting signals.
- The limits kept by design are listed in the package docstring, and the corpus tags the cases that show them `limitation`.

### The corpus is the specification

- `livecart_core/testing/corpus/comments.jsonl` holds 200 French and English comments (98 matches, 53 ambiguities, 49 rejections), each with its exact result against the demo catalog. The corpus test compares every field (kind, reason, code, variant, quantity, confidence, candidates), lists every case that differs, and holds the corpus within the plan's bounds (150 to 200 cases, 40 ambiguous at least) with every reason and every confidence present.
- Unit tests cover each rule, and seeded property tests check what no case list can: random comments of up to 1000 code points never raise and never match, and comments built from real pieces keep every invariant (the evidence quotes the comment in order, the quantity is in bounds, the variant belongs to the product).
- The same corpus is the evaluation set that any change of model must pass in milestone 4c (`scripts/llm_eval.py`).

### The fallback sits behind a port, and its answer is not trusted

- `IntentFallback` (`livecart_core.application.ports`) receives the comment's text, the ambiguity, the catalog and the time left (`FallbackRequest`), never the buyer's identity. It never raises for a failure of the model or its provider: it returns a receipt with a closed `LlmOutcome` (`ok`, `disabled`, `budget_exceeded`, `llm_busy`, `timeout`, `error`, `refused`, `truncated`, `invalid`), and only an `ok` receipt carries a proposal.
- The proposal (`LlmProposal`) is checked against the catalog by the domain before anything is reserved (`decide_from_llm`, milestone 4c), so a code or a variant it names may name nothing.
- `NullIntentFallback` is the fallback of a deployment without an LLM: every ambiguity then ends as an `ambiguous_unresolved` rejection.
- The Claude adapter, its structured outputs, the budget per session and per day, the bounded slot and the choice of model are decided and recorded here in milestone 4c.

## Consequences

- Only the four ambiguity reasons ever reach the fallback; the clear orders and the plain rejections need no network call, and `NullIntentFallback` lets a deployment run without any model.
- A phrasing the lexicon misses ends as a rejection, which never reaches the fallback, or as an ambiguity, which costs a call: the review of milestone 2 found such gaps ("jprends", "x-1", "15,90 €", "j'ai commandé") and each fix added its case to the corpus or to the unit tests. A new phrasing is a lexicon change checked against all 200 cases.
- The rejection reasons stay a closed list that the metrics and the dashboard can rely on; a bare number above the bound is no longer counted as `quantity_out_of_bounds`, since it may be a date or a price.
- The evidence lets the seller, and a test, see which words a result rests on.
- The lexicon, the guards of the typo tolerance and the decision order must be kept consistent by hand; the corpus test and the property tests are what keeps them honest.

## Alternatives considered

- **The model for every comment**: the cost, the latency and the provider's rate limits would hit hardest during a spike, and every order would depend on an output that must be checked anyway; the parser settles the clear cases alone.
- **One regular expression per phrasing**: the forms multiply with glued codes, typos, sizes and quantities in any order; a pass that gives each token one role composes them.
- **A fuzzy-matching library or a larger edit distance**: two edits, or no first-letter guard, read ordinary words as orders ("gold" for "sold", "deux" for "veux"); one edit under three guards is explainable and testable.
- **An NLP toolkit** (spaCy and its models): a heavy dependency outside the standard library of the domain, trained on neither live-shopping slang nor this catalog, and harder to make deterministic.
- **Rejecting every doubtful comment**: simpler, but it drops orders a model could recover; doubtful comments become ambiguities instead.
