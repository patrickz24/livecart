"""The comment webhook adapters: the simulator's, the documented stubs, and their startup."""

import importlib
from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError

from livecart_core.application.dto import CommentBatch, Platform
from livecart_core.application.ports import InvalidSignature, PlatformAdapter
from livecart_core.infrastructure.platforms import (
    STUBS,
    build_platform_adapter,
    build_platform_adapters,
)
from livecart_core.infrastructure.platforms.facebook import FacebookPlatformAdapter
from livecart_core.infrastructure.platforms.instagram import InstagramPlatformAdapter
from livecart_core.infrastructure.platforms.simulator import SimulatorPlatformAdapter
from livecart_core.infrastructure.platforms.tiktok import TikTokPlatformAdapter
from livecart_core.settings import Settings
from livecart_core.signing import SIGNATURE_HEADER, WebhookSigner
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FakeClock
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY

SECRET = SecretStr("simulator-webhook-" * 2)
BATCH = CommentBatch(
    comments=[
        incoming_comment("je prends A12 M", external_comment_id="c-1"),
        incoming_comment("SOLD B07 x2", external_comment_id="c-2", author="buyer-2"),
    ]
)


def _settings(**overrides: object) -> Settings:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://livecart_app:unit@db.invalid/livecart"),
        redis_url=SecretStr("redis://:unit@redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=SECRET,
        _env_file=None,
    )
    return settings.model_copy(update=overrides)


def _signed(body: bytes, clock: FakeClock, *, age_seconds: int = 0) -> dict[str, str]:
    timestamp = int(clock.now().timestamp()) - age_seconds
    return {SIGNATURE_HEADER: WebhookSigner(SECRET).header(body, timestamp=timestamp)}


# --- The simulator ---------------------------------------------------------------------


def test_the_simulator_adapter_verifies_the_signature_on_its_clock() -> None:
    clock = FakeClock()
    adapter = SimulatorPlatformAdapter(SECRET, clock)
    body = BATCH.model_dump_json().encode()
    headers = _signed(body, clock)

    adapter.verify(headers, body)
    # Header names are case-insensitive, as HTTP's are.
    adapter.verify({SIGNATURE_HEADER.lower(): headers[SIGNATURE_HEADER]}, body)
    clock.advance(timedelta(seconds=301))
    with pytest.raises(InvalidSignature):
        adapter.verify(headers, body)


def test_the_simulator_adapter_refuses_an_unsigned_or_forged_body() -> None:
    clock = FakeClock()
    adapter = SimulatorPlatformAdapter(SECRET, clock)
    body = BATCH.model_dump_json().encode()
    forged = WebhookSigner(SecretStr("guessed-webhook-key-" * 2))
    timestamp = int(clock.now().timestamp())

    with pytest.raises(InvalidSignature):
        adapter.verify({}, body)
    with pytest.raises(InvalidSignature):
        adapter.verify({SIGNATURE_HEADER: forged.header(body, timestamp=timestamp)}, body)
    with pytest.raises(InvalidSignature):
        adapter.verify(_signed(body, clock), body.replace(b"A12", b"A13"))


def test_the_simulator_adapter_reads_the_common_format_as_is() -> None:
    adapter = SimulatorPlatformAdapter(SECRET, FakeClock())

    comments = adapter.parse(BATCH.model_dump_json().encode())

    assert adapter.platform is Platform.SIMULATOR
    assert comments == list(BATCH.comments)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b'{"comments": []}',
        b'{"comments": [], "extra": 1}',
        b'[{"text": "je prends A12"}]',
    ],
)
def test_the_simulator_adapter_refuses_what_is_no_batch_with_a_value_error(body: bytes) -> None:
    adapter = SimulatorPlatformAdapter(SECRET, FakeClock())

    with pytest.raises(ValidationError):
        adapter.parse(body)
    assert issubclass(ValidationError, ValueError)


# --- The stubs -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stub", "platform", "header"),
    [
        (FacebookPlatformAdapter, Platform.FACEBOOK, "X-Hub-Signature-256"),
        (InstagramPlatformAdapter, Platform.INSTAGRAM, "X-Hub-Signature-256"),
        (TikTokPlatformAdapter, Platform.TIKTOK, "TikTok-Signature"),
    ],
)
def test_each_stub_documents_its_signature_header_and_refuses_to_be_built(
    stub: type[FacebookPlatformAdapter | InstagramPlatformAdapter | TikTokPlatformAdapter],
    platform: Platform,
    header: str,
) -> None:
    assert stub.signature_header == header
    assert STUBS[platform] is stub
    # The module documents how the platform signs, for the adapter to come.
    assert header in (importlib.import_module(stub.__module__).__doc__ or "")

    with pytest.raises(NotImplementedError, match=f"{platform}.*ENABLED_PLATFORMS"):
        stub()


def test_the_stubs_cover_every_platform_but_the_simulator() -> None:
    assert set(STUBS) == set(Platform) - {Platform.SIMULATOR}


# --- Startup ---------------------------------------------------------------------------


def test_the_default_settings_build_the_simulator_adapter_only() -> None:
    adapters = build_platform_adapters(_settings(), FakeClock())

    assert list(adapters) == [Platform.SIMULATOR]
    adapter: PlatformAdapter = adapters[Platform.SIMULATOR]
    assert isinstance(adapter, SimulatorPlatformAdapter)


def test_the_simulator_needs_its_secret_to_start() -> None:
    with pytest.raises(ValueError, match="COMMENT_WEBHOOK_SECRET_SIMULATOR must be set"):
        build_platform_adapters(_settings(comment_webhook_secret_simulator=None), FakeClock())


@pytest.mark.parametrize("platform", sorted(STUBS))
def test_an_enabled_stub_stops_the_startup_even_with_a_secret(platform: Platform) -> None:
    settings = _settings(
        enabled_platforms=(Platform.SIMULATOR, platform),
        **{f"comment_webhook_secret_{platform.value}": SECRET},
    )

    with pytest.raises(NotImplementedError, match="documented stub"):
        build_platform_adapters(settings, FakeClock())
    # Before any secret is read: the error names the real problem.
    with pytest.raises(NotImplementedError):
        build_platform_adapter(platform, _settings(), FakeClock())
