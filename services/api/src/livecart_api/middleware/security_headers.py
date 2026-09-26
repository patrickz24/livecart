"""Security headers on every response of the API, errors included.

The API answers JSON to scripts of its own origin: nothing it sends is meant
to be sniffed as another type, rendered as a page, framed or followed by a
referrer. So every response carries:

- ``X-Content-Type-Options: nosniff``: a JSON body is never run as a script;
- ``X-Frame-Options: DENY`` and the CSP's ``frame-ancestors 'none'``: no page
  may frame a response (the header for older browsers, the directive for the
  others);
- ``Content-Security-Policy: default-src 'none'``: were a response ever
  rendered, it could load and run nothing;
- ``Referrer-Policy: no-referrer``;
- ``Strict-Transport-Security`` when ``HSTS_ENABLED`` is set, once the API is
  only reached over HTTPS.

The interactive documentation of development (``/docs``, whose Swagger UI
page runs an inline script and loads its bundle from a CDN) keeps every
header but the CSP. Production serves no documentation.

The few HTML pages the API renders for a browser (``livecart_api.pages``:
the payment bridge, the mock provider's hosted page) set a CSP of their own,
as strict for a page (``default-src 'none'``, their one stylesheet by its
hash), which this middleware keeps; every other response gets the one above.
"""

from collections.abc import Iterable
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_CSP: Final = "default-src 'none'; frame-ancestors 'none'"
# A year, the lifetime browsers and the preload list expect.
HSTS: Final = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Set the headers of the module docstring on every HTTP response."""

    def __init__(self, app: ASGIApp, *, hsts: bool, csp_exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": API_CSP,
        }
        if hsts:
            headers["Strict-Transport-Security"] = HSTS
        self._headers = headers
        self._page_headers = {
            name: value for name, value in headers.items() if name != "Content-Security-Policy"
        }
        self._csp_exempt_paths = frozenset(csp_exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = self._page_headers if scope["path"] in self._csp_exempt_paths else self._headers

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in headers.items():
                    if name == "Content-Security-Policy":
                        # A page's own, as strict for what it renders; see the docstring.
                        response_headers.setdefault(name, value)
                    else:
                        response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
