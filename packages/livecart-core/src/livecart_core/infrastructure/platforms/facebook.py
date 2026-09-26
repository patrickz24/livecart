"""Facebook Live comments, through the webhooks of Meta's Graph API: a documented stub.

How Meta signs a webhook delivery, which the real adapter's ``verify`` must
check on the raw body before anything else:

- the header ``X-Hub-Signature-256: sha256=<hex>`` carries the HMAC-SHA256
  of the exact body bytes under the app secret
  (``COMMENT_WEBHOOK_SECRET_FACEBOOK``), compared in constant time;
- the header holds no timestamp, so a replay is not refused by the
  signature: the inbox's unique (platform, external_comment_id) makes it
  harmless;
- subscribing the endpoint is a separate ``GET`` handshake (``hub.mode``,
  ``hub.verify_token``, ``hub.challenge``), which the stub does not serve.

``parse`` would then map the Graph API's comment change (the comment id, the
live video id, the author's page-scoped id, the message and its creation
time) onto ``IncomingComment``.
"""

from typing import ClassVar

from livecart_core.application.dto import Platform
from livecart_core.infrastructure.platforms.stub import PlatformStub


class FacebookPlatformAdapter(PlatformStub):
    """Refuses to be built: see the module docstring for the signature to verify."""

    signature_header: ClassVar[str] = "X-Hub-Signature-256"
    _platform: ClassVar[Platform] = Platform.FACEBOOK
