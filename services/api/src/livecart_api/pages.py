"""The few HTML pages the API renders for a browser, and the strict CSP they share.

Everything else the API answers is JSON. A payment happens in the buyer's
browser, though: the provider's hosted page sends it back to the bridge page
(``GET /api/v1/payments/return``), and in development the mock provider's
hosted page is served here too (``/api/v1/mock-payments/{ref}``). Those
pages:

- are plain HTML, without a script: a link or a form button is all they
  need, and a tap on it is the user gesture a browser asks for before it
  opens an app;
- carry ``Content-Security-Policy: default-src 'none'``, which allows their
  one stylesheet by its SHA-256 and nothing else, no base URL, no frame
  around them, and forms only towards the API's own origin (``form-action
  'self'``) or none at all;
- escape every value they show, whatever its source (``html.escape``, quotes
  included, for text and attributes alike), and are never cached.

They are for browsers, not for the generated client: their routes stay out
of the OpenAPI document.
"""

import base64
import hashlib
from decimal import Decimal
from html import escape
from typing import Final

from fastapi.responses import HTMLResponse

from livecart_core.domain.money import Money

STYLE: Final = (
    "body{font-family:system-ui,sans-serif;max-width:28rem;margin:2.5rem auto;"
    "padding:0 1rem;line-height:1.5;color:#1b1f24;background:#fff}"
    "h1{font-size:1.4rem}"
    ".note{padding:.5rem .75rem;border-radius:.4rem;background:#fff4e5}"
    ".button{display:block;box-sizing:border-box;width:100%;margin:1rem 0;"
    "padding:.8rem 1rem;border:0;border-radius:.5rem;background:#1f6feb;color:#fff;"
    "font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}"
    ".secondary{background:#eaeef2;color:#1b1f24}"
    "form{margin:0}"
)
STYLE_HASH: Final = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()


def page_csp(*, form_action: str) -> str:
    """The CSP of a page: nothing but its stylesheet, and forms towards ``form_action`` only."""
    return (
        f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; form-action {form_action}; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def text(value: object) -> str:
    """``value`` as HTML text or attribute value: every markup character escaped, quotes too."""
    return escape(str(value), quote=True)


def amount_text(amount: Money) -> str:
    """``25.00 EUR`` from 2500 minor units, for a page to show; two decimals, as EUR has."""
    return f"{Decimal(amount.amount_minor).scaleb(-2):.2f} {amount.currency}"


def html_page(
    title: str, body: str, *, status_code: int = 200, forms: bool = False
) -> HTMLResponse:
    """A page of ``body``, HTML its caller built with ``text`` around every value.

    ``forms`` lets the page post forms to the API's own origin; without it,
    the page may post none.
    """
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{text(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Content-Security-Policy": page_csp(form_action="'self'" if forms else "'none'"),
            "Cache-Control": "no-store",
        },
    )
