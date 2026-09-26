# Comment corpus

`comments.jsonl` holds the French and English live comments the purchase-intent parser (`livecart_core.domain.parser`) is tested against, one case per line, with the exact result it must give. `tests/unit/domain/test_parser_corpus.py` parses every case and fails as soon as one field of one case differs, listing every failing line.

Every expectation assumes the demo catalog, `livecart_core.testing.builders.demo_catalog()`, and a bound of 10 per comment (`DEMO_MAX_QUANTITY`, the default `MAX_QUANTITY_PER_COMMENT`):

| Code  | Name             | Variants                     |
| ----- | ---------------- | ---------------------------- |
| `A12` | Robe fleurie     | S, M, L                      |
| `B07` | Sac en cuir      | none                         |
| `C3`  | T-shirt coton    | Rouge, Bleu, Bleu nuit, Noir |
| `D4`  | Jean droit       | 36, 38, 40, 42               |
| `E15` | Casquette brodée | Unique                       |
| `Z99` | Carte cadeau     | none                         |

Changing that catalog means reviewing the whole corpus.

## Format

Each line is one JSON object, in UTF-8. Visible characters are written as they are (`é`, `🛒`, `Ｃ３`); invisible ones are written as `\u` escapes, so that a keycap two reads `2\ufe0f\u20e3` rather than looking like a plain `2`. The fields depend on `kind`:

| `kind`      | Fields besides `text` and `tag`                                                                               |
| ----------- | ------------------------------------------------------------------------------------------------------------- |
| `match`     | `code`, `variant` (`""` when the product has none), `quantity`, `confidence` (`"1.00"`, `"0.90"` or `"0.80"`) |
| `ambiguous` | `reason` (an `AmbiguityReason`), `candidates` (catalog codes, possibly `[]`), `resolution` (below)            |
| `rejected`  | `reason` (a `RejectionReason` the parser decides)                                                             |

```json
{"text": "SOLD A12 M x2", "kind": "match", "code": "A12", "variant": "M", "quantity": 2, "confidence": "1.00", "tag": "en-verb"}
{"text": "slod C3", "kind": "ambiguous", "reason": "unresolved_variant", "candidates": ["C3"], "resolution": null, "tag": "typo"}
{"text": "je prends deux B07", "kind": "ambiguous", "reason": "conflicting_signals", "candidates": ["B07"], "resolution": {"code": "B07", "variant": "", "quantity": 2}, "tag": "number-word"}
{"text": "C3 dispo ?", "kind": "rejected", "reason": "no_intent", "tag": "question"}
```

Every field of the parser's result is compared exactly: the kind, the reason, the code, the variant label, the quantity, the confidence and the list of candidates. The evidence is not part of the corpus; the unit tests of the parser check it.

The `resolution` of an ambiguous case is not the parser's: it is the order a careful reader takes from the comment, which the LLM fallback should settle, or `null` when the comment orders nothing the catalog can take (a question on the order itself, a negation, a product with several variants and none named, a quantity that contradicts itself or exceeds 10, a code the catalog lacks). `"B7"` resolves to `B07`, a code typed without its zero; `"je prends Z99 si dispo"` and `"je prends A12 M c'est combien ?"` resolve to their order, whose condition or question does not undo it. `scripts/llm_eval.py` compares the fallback's decisions with these labels, and a test checks that each resolution is an order the domain accepts from the model (`decide_from_llm`).

`tag` is one short lower-case word (letters, digits, hyphens) that says what the case exercises: `fr-verb`, `en-verb`, `typo`, `typo-guard` (an ordinary word that must not pass for a verb typo), `emoji`, `fullwidth`, `case`, `accent`, `apostrophe`, `elision`, `variant`, `size`, `colour`, `synonym`, `single-variant`, `no-variant`, `glued`, `quantity`, `number-word`, `bounds`, `price`, `time`, `question`, `negation`, `past` (a purchase already made), `noise`, `mention`, `multiple`, `code-alone`, `o0-confusion`, `unknown-code`, `name`, and `limitation` for a result that is deliberate but that a reader might not expect (the docstring of `livecart_core.domain.parser` lists those limits).

## Adding a case

1. Write the comment as a buyer would type it, and work out the expected result from the decision order in the docstring of `livecart_core.domain.parser`, not from what the parser returns today.
2. Append one line to `comments.jsonl`, with a tag from the list above (or a new one, added to the list).
3. Run `uv run --locked pytest packages/livecart-core/tests/unit/domain/test_parser_corpus.py`. A failure prints the line number, the tag, the text, the expected fields and the actual ones.
4. If the parser is wrong, fix the parser and add a focused unit test for the rule; if the expectation is wrong, say why in the commit message.

The plan keeps the corpus between 150 and 200 cases, at least 40 of them ambiguous, and the test enforces both bounds. A text appears once. Keep the ambiguous cases precise: they are the only comments the LLM fallback sees, so each should be one the parser truly cannot settle alone, and each needs its `resolution`.
