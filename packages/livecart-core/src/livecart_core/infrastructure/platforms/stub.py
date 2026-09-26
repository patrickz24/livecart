"""The shape shared by the adapters of the real platforms, which are documented stubs.

Integrating Facebook, Instagram or TikTok is out of the MVP's scope. Each of
them still has its class, which names the platform and documents how its
webhooks are signed, so that the real adapter knows what ``verify`` must
check. Building one raises ``NotImplementedError``: listed in
``ENABLED_PLATFORMS``, a stub stops the API at startup rather than accept
comments it cannot verify.
"""

from collections.abc import Mapping
from typing import ClassVar

from livecart_core.application.dto import IncomingComment, Platform


class PlatformStub:
    """A platform adapter that is designed but not implemented; see the module docstring."""

    # The platform's own signature header, which the real adapter will verify.
    signature_header: ClassVar[str]
    _platform: ClassVar[Platform]

    def __init__(self) -> None:
        raise NotImplementedError(
            f"the {self._platform} comment webhook is a documented stub, not implemented: "
            f"remove {self._platform} from ENABLED_PLATFORMS"
        )

    @property
    def platform(self) -> Platform:
        return self._platform

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        raise NotImplementedError(f"the {self._platform} adapter is a stub")

    def parse(self, raw_body: bytes) -> list[IncomingComment]:
        raise NotImplementedError(f"the {self._platform} adapter is a stub")
