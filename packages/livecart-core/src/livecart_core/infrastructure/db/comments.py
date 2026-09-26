"""``CommentInbox`` on PostgreSQL: the ``comments`` table, its deduplication and its lease.

Every method is one short READ COMMITTED transaction of SQLAlchemy Core
constructs, and every write that settles a comment carries the guard
``status = 'received'``:

- ``claim``, unit 1, one statement::

      INSERT INTO comments (...) VALUES (...)
      ON CONFLICT (platform, external_comment_id) DO UPDATE
        SET claimed_by = excluded.claimed_by, claimed_until = excluded.claimed_until
        WHERE comments.status = 'received'
          AND (comments.claimed_until IS NULL OR comments.claimed_until < :now
               OR comments.claimed_by = :holder)
      RETURNING ...

  A row back: the holder has the lease, of a new comment or of a known one
  whose lease ended (or was its own). No row: another delivery holds the
  lease, or the comment is settled, and the row is read as it stands. Of
  concurrent claims of a new comment, the first ``INSERT`` wins; the others
  wait for it on the unique index, then evaluate the ``WHERE`` on the row it
  committed and find its lease running. A known comment keeps the text of its
  first delivery: only the lease columns are ever updated;
- ``record_llm_receipt``, unit 2: ``UPDATE ... WHERE id = :id AND
  llm_outcome IS NULL``, then the receipt stored is read back, whichever
  attempt wrote it;
- ``reject``: a compare-and-set from ``received``; no row updated, the
  status read back decides between ``NOOP`` and ``InvalidTransition``, as
  ``transition`` does for every machine;
- ``dead_letter``: the same compare-and-set, written ``INSERT ... ON
  CONFLICT DO UPDATE SET status = 'dead_lettered' WHERE comments.status =
  'received'``, so that a comment whose every delivery failed before unit 1
  is recorded, dead-lettered, too.

Every time comes from the caller's ``now`` (the ``Clock`` port).
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import (
    CommentClaim,
    FallbackReceipt,
    InboxComment,
    ParseKind,
    ParseSummary,
    Platform,
    QueuedComment,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.llm_decision import LlmProposal
from livecart_core.domain.outcomes import AmbiguityReason, LlmOutcome, LlmReason, RejectionReason
from livecart_core.domain.state_machines import (
    COMMENT_TRANSITIONS,
    CommentStatus,
    TransitionOutcome,
    transition,
)
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import comments
from livecart_core.infrastructure.db.units import transaction

DEDUPLICATION_CONSTRAINT = "uq_comments_platform_external_comment_id"


def parse_values(parse: ParseSummary | None) -> dict[str, Any]:
    """The ``parse_*`` columns of a settlement; none at all when the parser did not run."""
    if parse is None:
        return {}
    return {
        "parse_result": parse.kind.value,
        "parse_reason": None if parse.reason is None else parse.reason.value,
    }


def _parse_summary(row: Row[Any]) -> ParseSummary | None:
    if row.parse_result is None:
        return None
    kind = ParseKind(row.parse_result)
    if kind is ParseKind.MATCH:
        return ParseSummary(kind)
    reason_type = AmbiguityReason if kind is ParseKind.AMBIGUOUS else RejectionReason
    return ParseSummary(kind, reason_type(row.parse_reason))


def _llm_receipt(row: Row[Any]) -> FallbackReceipt:
    """The receipt of a row whose ``llm_outcome`` is set."""
    outcome = LlmOutcome(row.llm_outcome)
    proposal = None
    if outcome is LlmOutcome.OK:
        proposal = LlmProposal(
            reason=LlmReason(row.llm_reason),
            product_code=row.llm_product_code,
            variant=row.llm_variant,
            quantity=row.llm_quantity,
            confidence=row.llm_confidence,
        )
    return FallbackReceipt(
        outcome,
        proposal,
        request_id=row.llm_request_id,
        model=row.llm_model,
        input_tokens=row.llm_input_tokens,
        output_tokens=row.llm_output_tokens,
        latency_ms=row.llm_latency_ms,
    )


def inbox_comment(row: Row[Any]) -> InboxComment:
    return InboxComment(
        id=row.id,
        platform=Platform(row.platform),
        external_comment_id=row.external_comment_id,
        author_platform_user_id=row.author_platform_user_id,
        text=row.text,
        correlation_id=row.correlation_id,
        status=CommentStatus(row.status),
        created_at=row.created_at,
        session_id=row.session_id,
        claimed_by=row.claimed_by,
        claimed_until=row.claimed_until,
        rejection_reason=(
            None if row.rejection_reason is None else RejectionReason(row.rejection_reason)
        ),
        parse=_parse_summary(row),
        llm_receipt=None if row.llm_outcome is None else _llm_receipt(row),
    )


def _receipt_values(receipt: FallbackReceipt) -> dict[str, Any]:
    proposal = receipt.proposal
    return {
        "llm_outcome": receipt.outcome.value,
        "llm_reason": None if proposal is None else proposal.reason.value,
        "llm_product_code": None if proposal is None else proposal.product_code,
        "llm_variant": None if proposal is None else proposal.variant,
        "llm_quantity": None if proposal is None else proposal.quantity,
        "llm_confidence": None if proposal is None else proposal.confidence,
        "llm_request_id": receipt.request_id,
        "llm_model": receipt.model,
        "llm_input_tokens": receipt.input_tokens,
        "llm_output_tokens": receipt.output_tokens,
        "llm_latency_ms": receipt.latency_ms,
    }


class SqlCommentInbox:
    """The comment inbox; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim(
        self, comment: QueuedComment, *, holder: str, now: datetime, lease: timedelta
    ) -> CommentClaim:
        if lease <= timedelta(0):
            raise ValueError(f"lease must be positive, not {lease}")
        incoming = comment.comment
        new = insert(comments).values(
            id=new_id(),
            platform=incoming.platform.value,
            external_comment_id=incoming.external_comment_id,
            author_platform_user_id=incoming.author_platform_user_id,
            text=incoming.text,
            correlation_id=comment.correlation_id,
            status=CommentStatus.RECEIVED.value,
            claimed_by=holder,
            claimed_until=now + lease,
            created_at=now,
        )
        statement = new.on_conflict_do_update(
            constraint=DEDUPLICATION_CONSTRAINT,
            # ON CONFLICT DO UPDATE applies no onupdate default: updated_at is set here.
            set_={
                "claimed_by": new.excluded.claimed_by,
                "claimed_until": new.excluded.claimed_until,
                "updated_at": func.now(),
            },
            where=(comments.c.status == CommentStatus.RECEIVED.value)
            & (
                comments.c.claimed_until.is_(None)
                | (comments.c.claimed_until < now)
                | (comments.c.claimed_by == holder)
            ),
        ).returning(*comments.c)
        async with transaction(self._engine) as connection:
            leased = (await connection.execute(statement)).one_or_none()
            if leased is not None:
                return CommentClaim(acquired=True, comment=inbox_comment(leased))
            known = (
                await connection.execute(
                    select(comments).where(
                        comments.c.platform == incoming.platform.value,
                        comments.c.external_comment_id == incoming.external_comment_id,
                    )
                )
            ).one()
        return CommentClaim(acquired=False, comment=inbox_comment(known))

    async def get(self, comment_id: UUID) -> InboxComment | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(comments).where(comments.c.id == comment_id))
            ).one_or_none()
        return None if row is None else inbox_comment(row)

    async def record_llm_receipt(
        self, comment_id: UUID, receipt: FallbackReceipt
    ) -> FallbackReceipt:
        async with transaction(self._engine) as connection:
            await connection.execute(
                update(comments)
                .where(comments.c.id == comment_id, comments.c.llm_outcome.is_(None))
                .values(**_receipt_values(receipt))
            )
            row = (
                await connection.execute(select(comments).where(comments.c.id == comment_id))
            ).one_or_none()
        if row is None:
            raise RecordNotFound(f"no comment {comment_id}")
        # Set by now: by the update above, or by the attempt that came first.
        return _llm_receipt(row)

    async def reject(
        self,
        comment_id: UUID,
        reason: RejectionReason,
        *,
        session_id: UUID | None,
        parse: ParseSummary | None = None,
    ) -> TransitionOutcome:
        async with transaction(self._engine) as connection:
            settled = await connection.scalar(
                update(comments)
                .where(
                    comments.c.id == comment_id,
                    comments.c.status == CommentStatus.RECEIVED.value,
                )
                .values(
                    status=CommentStatus.REJECTED.value,
                    rejection_reason=reason.value,
                    session_id=session_id,
                    **parse_values(parse),
                )
                .returning(comments.c.id)
            )
            if settled is not None:
                return TransitionOutcome.APPLIED
            status = await self._status(connection, comments.c.id == comment_id)
        if status is None:
            raise RecordNotFound(f"no comment {comment_id}")
        return transition(COMMENT_TRANSITIONS, status, CommentStatus.REJECTED)

    async def dead_letter(self, comment: QueuedComment, *, now: datetime) -> TransitionOutcome:
        incoming = comment.comment
        new = insert(comments).values(
            id=new_id(),
            platform=incoming.platform.value,
            external_comment_id=incoming.external_comment_id,
            author_platform_user_id=incoming.author_platform_user_id,
            text=incoming.text,
            correlation_id=comment.correlation_id,
            status=CommentStatus.DEAD_LETTERED.value,
            created_at=now,
        )
        statement = new.on_conflict_do_update(
            constraint=DEDUPLICATION_CONSTRAINT,
            set_={"status": new.excluded.status, "updated_at": func.now()},
            where=comments.c.status == CommentStatus.RECEIVED.value,
        ).returning(comments.c.id)
        key = (comments.c.platform == incoming.platform.value) & (
            comments.c.external_comment_id == incoming.external_comment_id
        )
        async with transaction(self._engine) as connection:
            settled = await connection.scalar(statement)
            if settled is not None:
                return TransitionOutcome.APPLIED
            status = await self._status(connection, key)
        if status is None:
            # A conflict without a row: only a reset of the session, as the migrator, deletes one.
            raise RecordNotFound(f"no comment {incoming.platform}/{incoming.external_comment_id}")
        return transition(COMMENT_TRANSITIONS, status, CommentStatus.DEAD_LETTERED)

    @staticmethod
    async def _status(
        connection: AsyncConnection, key: ColumnElement[bool]
    ) -> CommentStatus | None:
        status = await connection.scalar(select(comments.c.status).where(key))
        return None if status is None else CommentStatus(status)
