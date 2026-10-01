"""``SyncShipmentTracking``: the shipments in flight, asked about at the carrier, each move told.

The plan's tracking loop, which every worker's shipments loop runs after its
fallback, every ``SHIPMENT_SYNC_INTERVAL_S`` (60 s):

1. a lease (``ShipmentRepository.lease_for_tracking``): at most 50
   shipments in flight whose lease has ended, ``FOR UPDATE SKIP LOCKED``,
   each leased for the interval, so that one worker alone asks the carrier
   about it once an interval, however many workers run; the unit commits
   before any call;
2. for each, one after the other and holding no connection during the
   call: the carrier's ``get_tracking``, then the transitions from the
   status recorded to the status reported (``shipment_steps``). A carrier
   reports where a shipment is, not every step it took: one found two steps
   further walks through the step in between, each step stamped with the
   time of the carrier's latest event;
3. each transition is one compare-and-set (``ShipmentRepository.set_status``)
   and, once it committed, one ``shipment.updated`` on its session's stream,
   only when that compare-and-set applied it: exactly one event per
   transition, whichever worker applied it. A step another worker applied
   first (``NOOP``) is skipped, and one it passed already
   (``InvalidTransition``) ends the walk there (``stale``).

A status the machine cannot reach from the recorded one (back along the
route, a cancellation in transit) is a carrier's bug: ``failed``, which the
worker logs at ERROR, and asks about again once the lease ends, as it does
a carrier or a database down (``retry``). A claim's work ends
``TRACKING_LEASE_MARGIN`` (5 s) before its lease does, so that no other
worker takes a shipment this one still follows; one it did not reach in
time is a ``retry`` too. A tick leases again while its claims come back
full, within its budget.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from livecart_core.application.create_shipment import CARRIER_CALL_TIMEOUT
from livecart_core.application.dto import LeasedShipment, ShipmentRecord
from livecart_core.application.ports import (
    CarrierProvider,
    Clock,
    EventPublisher,
    ServiceUnavailable,
    ShipmentRepository,
)
from livecart_core.application.session_events import shipment_updated
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.shipments import shipment_steps
from livecart_core.domain.state_machines import TransitionOutcome

# The most shipments one lease takes.
TRACKING_BATCH: Final = 50
# What a claim's work leaves of its lease, at least, before another worker may take it.
TRACKING_LEASE_MARGIN: Final = timedelta(seconds=5)


class TrackingOutcome(StrEnum):
    """What asking the carrier about one shipment came to: the label of the worker's metric."""

    # The carrier reports the status recorded already.
    UNCHANGED = "unchanged"
    # One transition or more applied by this worker, each told once.
    MOVED = "moved"
    # Another worker applied the reported moves first.
    STALE = "stale"
    # The carrier or the database could not be reached, or time ran out: after the lease.
    RETRY = "retry"
    # A status the machine cannot reach, or a ref the carrier does not know: needs a person.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TrackedShipment:
    """One leased shipment and what asking its carrier came to.

    ``moves`` holds the shipment after each transition this worker applied,
    in order, whatever ended the walk; ``error`` is what kept a ``retry``
    or a ``failed`` shipment from its reported status.
    """

    leased: LeasedShipment
    outcome: TrackingOutcome
    moves: tuple[ShipmentRecord, ...] = ()
    unpublished_events: int = 0
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class TrackingTick:
    """One tick of the tracking: every shipment it leased, in order, and its claims."""

    tracked: tuple[TrackedShipment, ...] = ()
    claims: int = 0

    def count(self, outcome: TrackingOutcome) -> int:
        return sum(1 for shipment in self.tracked if shipment.outcome is outcome)


@dataclass(slots=True)
class _Walk:
    """What one shipment's walk did so far: what survives a failure midway."""

    moves: list[ShipmentRecord]
    unpublished: int = 0


class SyncShipmentTracking:
    """One tick of the tracking loop; see the module docstring."""

    def __init__(
        self,
        shipments: ShipmentRepository,
        carrier: CarrierProvider,
        events: EventPublisher,
        clock: Clock,
        *,
        lease: timedelta,
        budget: timedelta,
        batch: int = TRACKING_BATCH,
        call_timeout: timedelta = CARRIER_CALL_TIMEOUT,
    ) -> None:
        if lease <= TRACKING_LEASE_MARGIN or budget <= timedelta(0):
            raise ValueError(
                f"a tick needs a time budget, and a lease longer than {TRACKING_LEASE_MARGIN}"
            )
        if batch < 1 or call_timeout <= timedelta(0):
            raise ValueError("a lease takes one shipment at least, and a carrier's call needs time")
        self._shipments = shipments
        self._carrier = carrier
        self._events = events
        self._clock = clock
        self._lease = lease
        self._budget = budget
        self._batch = batch
        self._call_timeout = call_timeout

    async def __call__(self) -> TrackingTick:
        """Lease and follow until a lease comes back short or the budget is spent.

        A lease that fails raises (``ServiceUnavailable`` when PostgreSQL is
        down): its shipments wait for the next tick. Whatever happens to one
        shipment is its own result, never another's.
        """
        started = self._clock.now()
        tracked: list[TrackedShipment] = []
        claims = 0
        while True:
            leased = await self._shipments.lease_for_tracking(
                now=self._clock.now(), limit=self._batch, lease=self._lease
            )
            claims += 1
            # The claim's work ends before its lease, by the loop's own clock.
            deadline = (
                asyncio.get_running_loop().time()
                + (self._lease - TRACKING_LEASE_MARGIN).total_seconds()
            )
            for item in leased:
                tracked.append(await self._track(item, deadline))
            if len(leased) < self._batch or self._clock.now() - started >= self._budget:
                return TrackingTick(tuple(tracked), claims)

    async def _track(self, leased: LeasedShipment, deadline: float) -> TrackedShipment:
        walk = _Walk([])
        try:
            async with asyncio.timeout_at(deadline):
                outcome = await self._follow(leased, walk)
        except (ServiceUnavailable, TimeoutError) as error:
            return self._result(leased, TrackingOutcome.RETRY, walk, error)
        except Exception as error:
            return self._result(leased, TrackingOutcome.FAILED, walk, error)
        return self._result(leased, outcome, walk)

    async def _follow(self, leased: LeasedShipment, walk: _Walk) -> TrackingOutcome:
        shipment = leased.shipment
        async with asyncio.timeout(self._call_timeout.total_seconds()):
            update = await self._carrier.get_tracking(shipment.ref)
        # A status the machine cannot reach raises InvalidTransition: failed.
        steps = shipment_steps(shipment.status, update.status)
        if not steps:
            return TrackingOutcome.UNCHANGED
        for step in steps:
            try:
                change = await self._shipments.set_status(
                    shipment.id, step, event_at=update.last_event_at
                )
            except InvalidTransition:
                # Another worker moved the shipment past this step: the rest is its own.
                break
            if change.outcome is TransitionOutcome.NOOP:
                continue
            walk.moves.append(change.shipment)
            walk.unpublished += await self._publish(leased, change.shipment)
        return TrackingOutcome.MOVED if walk.moves else TrackingOutcome.STALE

    async def _publish(self, leased: LeasedShipment, shipment: ShipmentRecord) -> int:
        """One transition on its session's stream, after its commit; 1 when that failed."""
        try:
            await self._events.publish(
                shipment_updated(
                    leased.session_id,
                    shipment,
                    correlation_id=None,
                    occurred_at=self._clock.now(),
                )
            )
        except ServiceUnavailable:
            return 1
        return 0

    @staticmethod
    def _result(
        leased: LeasedShipment,
        outcome: TrackingOutcome,
        walk: _Walk,
        error: Exception | None = None,
    ) -> TrackedShipment:
        return TrackedShipment(leased, outcome, tuple(walk.moves), walk.unpublished, error)
