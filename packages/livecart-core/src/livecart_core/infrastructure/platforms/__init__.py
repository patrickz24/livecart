"""The adapters of the comment webhook, one per platform (``PlatformAdapter``).

The simulator's is real; Facebook's, Instagram's and TikTok's are documented
stubs. ``build_platform_adapters`` builds those of ``ENABLED_PLATFORMS`` when
the API starts, so that a misconfiguration stops the process before it
listens: a stub raises ``NotImplementedError``, and the simulator without
``COMMENT_WEBHOOK_SECRET_SIMULATOR`` a ``ValueError``.
"""

from typing import Final

from pydantic import SecretStr

from livecart_core.application.dto import Platform
from livecart_core.application.ports import Clock, PlatformAdapter
from livecart_core.infrastructure.platforms.facebook import FacebookPlatformAdapter
from livecart_core.infrastructure.platforms.instagram import InstagramPlatformAdapter
from livecart_core.infrastructure.platforms.simulator import SimulatorPlatformAdapter
from livecart_core.infrastructure.platforms.stub import PlatformStub
from livecart_core.infrastructure.platforms.tiktok import TikTokPlatformAdapter
from livecart_core.settings import Settings

STUBS: Final[dict[Platform, type[PlatformStub]]] = {
    Platform.FACEBOOK: FacebookPlatformAdapter,
    Platform.INSTAGRAM: InstagramPlatformAdapter,
    Platform.TIKTOK: TikTokPlatformAdapter,
}


def _secret(settings: Settings, platform: Platform) -> SecretStr:
    secret = settings.comment_webhook_secret(platform)
    if secret is None:
        raise ValueError(
            f"COMMENT_WEBHOOK_SECRET_{platform.upper()} must be set while {platform} "
            "is in ENABLED_PLATFORMS"
        )
    return secret


def build_platform_adapter(platform: Platform, settings: Settings, clock: Clock) -> PlatformAdapter:
    """The adapter of one platform; a stub raises ``NotImplementedError`` before any secret."""
    if platform is Platform.SIMULATOR:
        return SimulatorPlatformAdapter(_secret(settings, platform), clock)
    return STUBS[platform]()


def build_platform_adapters(settings: Settings, clock: Clock) -> dict[Platform, PlatformAdapter]:
    """The adapter of each enabled platform, by platform; see the module docstring."""
    return {
        platform: build_platform_adapter(platform, settings, clock)
        for platform in settings.enabled_platforms
    }
