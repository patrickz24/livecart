"""The carriers ``CARRIER_PROVIDER`` selects: the stateless mock, its derivations, the stubs."""

import importlib
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from livecart_core.application.dto import (
    CarrierName,
    ShipmentRef,
    ShipmentRequest,
    ShippingAddress,
    TrackingUpdate,
)
from livecart_core.application.ports import CarrierProvider, RecordNotFound
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.infrastructure.carriers import STUBS, build_carrier
from livecart_core.infrastructure.carriers.chronopost import ChronopostCarrier
from livecart_core.infrastructure.carriers.dhl import DhlCarrier
from livecart_core.infrastructure.carriers.dpd import DpdCarrier
from livecart_core.infrastructure.carriers.mock import MOCK_CARRIER_STEP, MockCarrier
from livecart_core.infrastructure.carriers.stub import CarrierProviderStub
from livecart_core.settings import AppEnv, Settings
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.keys import (
    TEST_JWT_SECRET,
    TEST_MOCK_CARRIER_SECRET,
    TEST_ORDER_TOKEN_KEY,
)

ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)
ORDER_ID = UUID("01926f7e-8b1c-7a3e-9f00-0123456789ab")
TRACKING_NUMBER = re.compile(r"MOCK-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{16}")
CARRIER_REF = re.compile(r"mock_shp_[0-9a-f]{32}")


def _settings(**overrides: object) -> Settings:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://livecart_app:unit@db.invalid/livecart"),
        redis_url=SecretStr("redis://:unit@redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        mock_carrier_secret=TEST_MOCK_CARRIER_SECRET,
        _env_file=None,
    )
    return settings.model_copy(update=overrides)


def _parcel(order_id: UUID = ORDER_ID) -> ShipmentRequest:
    return ShipmentRequest(order_id, OrderReference("LC-7K3F9Q"), ADDRESS)


def _mock(clock: FakeClock, secret: SecretStr = TEST_MOCK_CARRIER_SECRET) -> MockCarrier:
    return MockCarrier(secret, clock)


# --- The selection at startup ----------------------------------------------------------


def test_the_mock_is_the_default_carrier() -> None:
    carrier = build_carrier(_settings(), FakeClock())

    assert isinstance(carrier, MockCarrier)
    assert carrier.name is CarrierName.MOCK
    assert TEST_MOCK_CARRIER_SECRET.get_secret_value() not in repr(carrier)
    # Two minutes a step, the settings' default and the mock's own.
    assert _settings().mock_carrier_step == MOCK_CARRIER_STEP == timedelta(seconds=120)


def test_the_mock_needs_its_key() -> None:
    with pytest.raises(ValueError, match="MOCK_CARRIER_SECRET must be set"):
        build_carrier(_settings(mock_carrier_secret=None), FakeClock())


async def test_the_mock_takes_its_step_from_the_settings() -> None:
    clock = FakeClock()
    carrier = build_carrier(_settings(mock_carrier_step_seconds=10), clock)
    ref = await carrier.create_shipment(_parcel())

    clock.advance(30)

    assert (await carrier.get_tracking(ref)).status is ShipmentStatus.DELIVERED


def test_production_takes_the_mock_carrier() -> None:
    carrier = build_carrier(_settings(app_env=AppEnv.PRODUCTION), FakeClock())

    assert isinstance(carrier, MockCarrier)


@pytest.mark.parametrize("carrier", [CarrierName.DPD, CarrierName.CHRONOPOST, CarrierName.DHL])
def test_a_stub_stops_the_worker_at_startup(carrier: CarrierName) -> None:
    with pytest.raises(NotImplementedError, match=f"the {carrier} carrier is a documented stub"):
        build_carrier(_settings(carrier_provider=carrier), FakeClock())


def test_every_stub_names_its_carrier_and_documents_its_flow() -> None:
    assert {
        CarrierName.DPD: DpdCarrier,
        CarrierName.CHRONOPOST: ChronopostCarrier,
        CarrierName.DHL: DhlCarrier,
    } == STUBS
    for carrier, stub in STUBS.items():
        module = importlib.import_module(stub.__module__)
        assert module.__doc__ is not None
        assert "create_shipment" in module.__doc__
        assert "get_tracking" in module.__doc__
        assert "cancel_shipment" in module.__doc__
        # The class attribute, readable without building the stub.
        assert stub._carrier is carrier


async def test_a_stub_answers_nothing_even_when_built_around_its_constructor() -> None:
    stub = object.__new__(DhlCarrier)
    carrier: CarrierProvider = stub
    ref = ShipmentRef(CarrierName.DHL, "ref", "number", FAKE_CLOCK_START)

    assert carrier.name is CarrierName.DHL
    with pytest.raises(NotImplementedError):
        await carrier.create_shipment(_parcel())
    with pytest.raises(NotImplementedError):
        await carrier.get_tracking(ref)
    with pytest.raises(NotImplementedError):
        await carrier.cancel_shipment(ref)
    assert isinstance(stub, CarrierProviderStub)


# --- The mock's derivations ------------------------------------------------------------


async def test_a_mock_shipment_is_named_by_hmacs_of_its_order() -> None:
    ref = await _mock(FakeClock()).create_shipment(_parcel())

    assert CARRIER_REF.fullmatch(ref.carrier_ref)
    assert TRACKING_NUMBER.fullmatch(ref.tracking_number)
    assert ref.created_at == FAKE_CLOCK_START
    # Nothing of the order shows in what the buyer sees.
    assert ORDER_ID.hex[:8] not in ref.tracking_number.lower()
    assert "7K3F9Q" not in ref.tracking_number


async def test_every_mock_with_the_key_names_the_same_shipment_and_no_other_key_does() -> None:
    clock = FakeClock()
    first = await _mock(clock).create_shipment(_parcel())
    clock.advance(45)

    restarted = await _mock(clock).create_shipment(_parcel())
    other_key = await _mock(clock, SecretStr("another-mock-carrier-key-" * 2)).create_shipment(
        _parcel()
    )

    assert (restarted.carrier_ref, restarted.tracking_number) == (
        first.carrier_ref,
        first.tracking_number,
    )
    assert restarted.created_at == first.created_at + timedelta(seconds=45)
    assert other_key.carrier_ref != first.carrier_ref
    assert other_key.tracking_number != first.tracking_number


@pytest.mark.parametrize(
    ("elapsed_s", "status", "since_s"),
    [
        (-60, ShipmentStatus.CREATED, 0),
        (0, ShipmentStatus.CREATED, 0),
        (119, ShipmentStatus.CREATED, 0),
        (120, ShipmentStatus.IN_TRANSIT, 120),
        (239, ShipmentStatus.IN_TRANSIT, 120),
        (240, ShipmentStatus.OUT_FOR_DELIVERY, 240),
        (359, ShipmentStatus.OUT_FOR_DELIVERY, 240),
        (360, ShipmentStatus.DELIVERED, 360),
        (86_400, ShipmentStatus.DELIVERED, 360),
    ],
)
async def test_the_mock_computes_the_status_from_the_creation_and_the_clock(
    elapsed_s: int, status: ShipmentStatus, since_s: int
) -> None:
    clock = FakeClock()
    carrier = _mock(clock)
    ref = await carrier.create_shipment(_parcel())

    clock.set(ref.created_at + timedelta(seconds=elapsed_s))

    assert timedelta(seconds=120) == MOCK_CARRIER_STEP
    assert await carrier.get_tracking(ref) == TrackingUpdate(
        status, ref.created_at + timedelta(seconds=since_s)
    )


async def test_the_mock_tracks_from_the_creation_the_ref_carries() -> None:
    clock = FakeClock()
    carrier = _mock(clock)
    recorded = await carrier.create_shipment(_parcel())
    clock.advance(200)
    # A later creation of the same shipment, which LiveCart does not record.
    await carrier.create_shipment(_parcel())

    assert await carrier.get_tracking(recorded) == TrackingUpdate(
        ShipmentStatus.IN_TRANSIT, recorded.created_at + MOCK_CARRIER_STEP
    )


@pytest.mark.parametrize(
    "forge",
    [
        lambda ref: replace(ref, tracking_number="MOCK-0000000000000000"),
        lambda ref: replace(ref, carrier_ref="mock_shp_" + "0" * 32),
        lambda ref: replace(ref, carrier_ref="dhl_00000000"),
        lambda ref: replace(ref, carrier=CarrierName.DHL),
    ],
    ids=["number", "ref", "prefix", "carrier"],
)
async def test_the_mock_knows_only_the_refs_it_issued(
    forge: Callable[[ShipmentRef], ShipmentRef],
) -> None:
    carrier = _mock(FakeClock())
    ref = await carrier.create_shipment(_parcel())

    with pytest.raises(RecordNotFound):
        await carrier.get_tracking(forge(ref))


async def test_the_mock_accepts_a_cancellation_until_the_parcel_is_in_transit() -> None:
    clock = FakeClock()
    carrier = _mock(clock)
    ref = await carrier.create_shipment(_parcel())

    await carrier.cancel_shipment(ref)
    clock.advance(MOCK_CARRIER_STEP)

    with pytest.raises(InvalidTransition):
        await carrier.cancel_shipment(ref)


def test_a_step_of_the_route_takes_some_time() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        MockCarrier(TEST_MOCK_CARRIER_SECRET, FakeClock(), step=timedelta(0))
