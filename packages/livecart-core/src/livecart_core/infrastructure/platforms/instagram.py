"""Instagram Live comments, through the webhooks of Meta's Graph API: a documented stub.

Instagram's webhooks are Meta's, signed as Facebook's are, which the real
adapter's ``verify`` must check on the raw body before anything else:

- the header ``X-Hub-Signature-256: sha256=<hex>`` carries the HMAC-SHA256
  of the exact body bytes under the app secret
  (``COMMENT_WEBHOOK_SECRET_INSTAGRAM``), compared in constant time;
- the header holds no timestamp: the inbox's unique (platform,
  external_comment_id) makes a replay harmless;
- subscribing the endpoint is a separate ``GET`` handshake (``hub.mode``,
  ``hub.verify_token``, ``hub.challenge``), which the stub does not serve.

``parse`` would then map the ``live_comments`` change (the comment id, the
live media id, the author's Instagram-scoped id and the text) onto
``IncomingComment``.
"""

from typing import ClassVar

from livecart_core.application.dto import Platform
from livecart_core.infrastructure.platforms.stub import PlatformStub


class InstagramPlatformAdapter(PlatformStub):
    """Refuses to be built: see the module docstring for the signature to verify."""

    signature_header: ClassVar[str] = "X-Hub-Signature-256"
    _platform: ClassVar[Platform] = Platform.INSTAGRAM
