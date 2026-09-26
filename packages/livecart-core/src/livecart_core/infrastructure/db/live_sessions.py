"""``LiveSessionRepository`` on PostgreSQL: a seller's sessions, their lifecycle and their catalog.

Every method that serves a seller filters its statements by
``live_sessions.seller_id``: another seller's session reads as missing.

- ``set_status`` is a compare-and-set, one statement::

      UPDATE live_sessions SET status = :target, started_at = :now
      WHERE id = :id AND seller_id = :seller AND status IN (:sources)
      RETURNING ...

  where the sources are the states the transition table leads from to the
  target. No row: the session is read again and ``transition`` decides
  between ``NOOP`` (already there) and ``InvalidTransition``. Statuses only
  move forward, so when the read shows that a concurrent change made the
  target reachable after all, the update is tried again, at most once per
  status.
- ``update`` locks the session's row with ``FOR NO KEY UPDATE`` before it
  checks what the status still allows, so a concurrent ``start`` waits and
  cannot slip between the check and the write. ``NO KEY`` leaves alone the
  ``FOR KEY SHARE`` locks that the foreign keys of comments, intents and
  orders take on the row: a reservation of the session never waits for it.
- ``uq_live_sessions_platform_external_live_id`` answers a taken platform
  and live id with ``LiveSessionConflict``.

Every time comes from the caller's ``now`` (the ``Clock`` port).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    LiveSessionChanges,
    LiveSessionRecord,
    LiveSessionTransition,
    NewLiveSession,
    Page,
    Platform,
    page_of,
)
from livecart_core.application.ports import LiveSessionConflict, RecordNotFound
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.live_sessions import check_session_edit
from livecart_core.domain.state_machines import (
    LIVE_SESSION_TRANSITIONS,
    LiveSessionStatus,
    TransitionOutcome,
    transition,
)
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import live_sessions, products
from livecart_core.infrastructure.db.units import transaction, violated_constraint

LIVE_ID_CONSTRAINT = "uq_live_sessions_platform_external_live_id"
SELLER_FOREIGN_KEY = "fk_live_sessions_seller_id_sellers"


def live_session_record(row: Row[Any]) -> LiveSessionRecord:
    return LiveSessionRecord(
        id=row.id,
        seller_id=row.seller_id,
        title=row.title,
        platform=Platform(row.platform),
        external_live_id=row.external_live_id,
        status=LiveSessionStatus(row.status),
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def owned_session(seller_id: UUID, session_id: UUID) -> ColumnElement[bool]:
    """The seller filter of every seller-scoped statement: this session, if it is this seller's."""
    return (live_sessions.c.id == session_id) & (live_sessions.c.seller_id == seller_id)


def _port_error(error: IntegrityError) -> Exception:
    """The port error an integrity error of ``live_sessions`` stands for, or the error itself."""
    constraint = violated_constraint(error)
    if constraint == LIVE_ID_CONSTRAINT:
        return LiveSessionConflict("another session has this platform and live id")
    if constraint == SELLER_FOREIGN_KEY:
        return RecordNotFound("no such seller")
    return error


class SqlLiveSessionRepository:
    """Live sessions; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self, seller_id: UUID, session: NewLiveSession, *, now: datetime
    ) -> LiveSessionRecord:
        try:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        insert(live_sessions)
                        .values(
                            id=new_id(),
                            seller_id=seller_id,
                            title=session.title,
                            platform=session.platform.value,
                            external_live_id=session.external_live_id,
                            status=LiveSessionStatus.DRAFT.value,
                            created_at=now,
                        )
                        .returning(*live_sessions.c)
                    )
                ).one()
        except IntegrityError as error:
            raise _port_error(error) from error
        return live_session_record(row)

    async def get(self, session_id: UUID) -> LiveSessionRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(live_sessions).where(live_sessions.c.id == session_id)
                )
            ).one_or_none()
        return None if row is None else live_session_record(row)

    async def get_for_seller(self, seller_id: UUID, session_id: UUID) -> LiveSessionRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(live_sessions).where(owned_session(seller_id, session_id))
                )
            ).one_or_none()
        return None if row is None else live_session_record(row)

    async def list_for_seller(
        self, seller_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[LiveSessionRecord]:
        query = select(live_sessions).where(live_sessions.c.seller_id == seller_id)
        if cursor is not None:
            # Newest first: the next page holds the sessions created before the cursor.
            query = query.where(live_sessions.c.id < cursor)
        async with transaction(self._engine) as connection:
            rows = (
                await connection.execute(query.order_by(live_sessions.c.id.desc()).limit(limit + 1))
            ).all()
        return page_of([live_session_record(row) for row in rows], limit)

    async def update(
        self, seller_id: UUID, session_id: UUID, changes: LiveSessionChanges
    ) -> LiveSessionRecord:
        values: dict[str, object] = {}
        if changes.title is not None:
            values["title"] = changes.title
        if changes.platform is not None:
            values["platform"] = changes.platform.value
        if changes.external_live_id is not None:
            values["external_live_id"] = changes.external_live_id
        try:
            async with transaction(self._engine) as connection:
                current = (
                    await connection.execute(
                        select(live_sessions)
                        .where(owned_session(seller_id, session_id))
                        .with_for_update(key_share=True)
                    )
                ).one_or_none()
                if current is None:
                    raise RecordNotFound(f"no live session {session_id} for this seller")
                session = live_session_record(current)
                check_session_edit(session.status, keys=changes.touches_keys(session))
                if not values:
                    return session
                row = (
                    await connection.execute(
                        update(live_sessions)
                        .where(owned_session(seller_id, session_id))
                        .values(**values)
                        .returning(*live_sessions.c)
                    )
                ).one()
        except IntegrityError as error:
            raise _port_error(error) from error
        return live_session_record(row)

    async def set_status(
        self, seller_id: UUID, session_id: UUID, target: LiveSessionStatus, *, now: datetime
    ) -> LiveSessionTransition:
        sources = sorted(current.value for current, to in LIVE_SESSION_TRANSITIONS if to is target)
        stamp = "started_at" if target is LiveSessionStatus.LIVE else "ended_at"
        # Each failed compare-and-set that a new read shows reachable is a status passed
        # by a concurrent change: there are no more tries than statuses.
        for _ in LiveSessionStatus:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        update(live_sessions)
                        .where(
                            owned_session(seller_id, session_id),
                            live_sessions.c.status.in_(sources),
                        )
                        .values(status=target.value, **{stamp: now})
                        .returning(*live_sessions.c)
                    )
                ).one_or_none()
                if row is None:
                    row = (
                        await connection.execute(
                            select(live_sessions).where(owned_session(seller_id, session_id))
                        )
                    ).one_or_none()
                    if row is None:
                        raise RecordNotFound(f"no live session {session_id} for this seller")
                    current = live_session_record(row)
                    if transition(LIVE_SESSION_TRANSITIONS, current.status, target) is (
                        TransitionOutcome.NOOP
                    ):
                        return LiveSessionTransition(current, TransitionOutcome.NOOP)
                    continue
            return LiveSessionTransition(live_session_record(row), TransitionOutcome.APPLIED)
        raise RuntimeError(f"live session {session_id} kept changing status")

    async def find_by_live_id(
        self, platform: Platform, external_live_id: str
    ) -> LiveSessionRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(live_sessions).where(
                        live_sessions.c.platform == platform.value,
                        live_sessions.c.external_live_id == external_live_id,
                    )
                )
            ).one_or_none()
        return None if row is None else live_session_record(row)

    async def load_catalog(self, session_id: UUID) -> Catalog:
        async with transaction(self._engine) as connection:
            known = await connection.scalar(
                select(live_sessions.c.id).where(live_sessions.c.id == session_id)
            )
            if known is None:
                raise RecordNotFound(f"no live session {session_id}")
            rows = (
                await connection.execute(
                    select(products.c.code, products.c.variant, products.c.name)
                    .where(products.c.session_id == session_id)
                    .order_by(products.c.id)
                )
            ).all()
        # A dict keeps the codes in the order of their first row, as the fake does.
        by_code: dict[str, list[Row[Any]]] = {}
        for row in rows:
            by_code.setdefault(row.code, []).append(row)
        return Catalog(
            tuple(
                CatalogProduct(
                    ProductCode(code),
                    same_code[0].name,
                    tuple(Variant(row.variant) for row in same_code if row.variant),
                )
                for code, same_code in by_code.items()
            )
        )
