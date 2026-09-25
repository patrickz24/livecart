"""Text normalisation shared by the value objects and, later, the parser."""

import unicodedata


def normalize_text(text: str) -> str:
    """NFKC compatibility form, then strip.

    NFKC folds the compatibility variants buyers' keyboards produce into the
    plain characters the catalogue uses: fullwidth letters and digits (the
    ``C3`` typed on a Japanese or Chinese keyboard), superscript and circled
    digits, and no-break spaces, which then fall to ``strip``.
    """
    return unicodedata.normalize("NFKC", text).strip()
