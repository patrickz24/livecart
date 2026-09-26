"""The simulator's comment webhook: the common format, signed with ``X-LiveCart-Signature``.

``livecart-simulate`` posts batches in the common format (``CommentBatch``)
and signs each body with ``COMMENT_WEBHOOK_SECRET_SIMULATOR`` through
``livecart_core.signing``, so this adapter only checks the signature and
validates the batch: no translation from a native format.
"""

from collections.abc import Mapping

from pydantic import SecretStr

from livecart_core.application.dto import CommentBatch, IncomingComment, Platform
from livecart_core.application.ports import Clock
from livecart_core.signing import SIGNATURE_HEADER, WebhookSigner, header_value


class SimulatorPlatformAdapter:
    """``PlatformAdapter`` of the simulator; time comes from ``clock`` for the 300 s tolerance."""

    def __init__(self, secret: SecretStr, clock: Clock) -> None:
        self._signer = WebhookSigner(secret)
        self._clock = clock

    @property
    def platform(self) -> Platform:
        return Platform.SIMULATOR

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        self._signer.verify(
            header_value(headers, SIGNATURE_HEADER), raw_body, now=self._clock.now().timestamp()
        )

    def parse(self, raw_body: bytes) -> list[IncomingComment]:
        # pydantic's ValidationError is a ValueError: the API's 422.
        return list(CommentBatch.model_validate_json(raw_body).comments)
