"""TikTok LIVE comments, through TikTok for Developers' webhooks: a documented stub.

How TikTok signs a webhook delivery, which the real adapter's ``verify``
must check on the raw body before anything else:

- the header ``TikTok-Signature: t=<unix>,s=<hex>`` carries the Unix time
  of signing and the HMAC-SHA256 of ``<t>.<body>`` under the app's client
  secret (``COMMENT_WEBHOOK_SECRET_TIKTOK``): the scheme of
  ``livecart_core.signing`` with another header, compared in constant time;
- the timestamp bounds replays, with the same tolerance as the simulator's.

``parse`` would then map TikTok's event (the comment id, the live room id,
the author's open id, the text and its time) onto ``IncomingComment``.
"""

from typing import ClassVar

from livecart_core.application.dto import Platform
from livecart_core.infrastructure.platforms.stub import PlatformStub


class TikTokPlatformAdapter(PlatformStub):
    """Refuses to be built: see the module docstring for the signature to verify."""

    signature_header: ClassVar[str] = "TikTok-Signature"
    _platform: ClassVar[Platform] = Platform.TIKTOK
