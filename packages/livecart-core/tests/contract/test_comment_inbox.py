"""``CommentInbox`` on every backend: the in-memory fake, then PostgreSQL.

Unit 1 (the lease), unit 2 (the fallback's receipt) and the settlements of a
comment that reserves nothing. Each scenario runs twice through the
``backend`` fixture; the PostgreSQL run is marked ``integration``.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    FallbackReceipt,
    LlmProposal,
    ParseKind,
    ParseSummary,
    Platform,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.outcomes import AmbiguityReason, LlmOutcome, RejectionReason
from livecart_core.domain.state_machines import CommentStatus, TransitionOutcome
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
LEASE = timedelta(seconds=30)
COMMENT = queued_comment("je prends A12 M", external_comment_id="comment-1", author="buyer-1")
UNKNOWN = UUID(int=404)


async def test_a_new_comment_is_recorded_with_its_lease(backend: ReservationBackend) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)

    row = claim.comment
    assert claim.acquired
    assert row.status is CommentStatus.RECEIVED
    assert (row.platform, row.external_comment_id) == (Platform.SIMULATOR, "comment-1")
    assert row.author_platform_user_id == "buyer-1"
    assert row.text == "je prends A12 M"
    assert row.correlation_id == COMMENT.correlation_id
    assert (row.claimed_by, row.claimed_until) == ("worker-1/1-0", NOW + LEASE)
    assert row.created_at == NOW
    assert (row.session_id, row.rejection_reason, row.parse, row.llm_receipt) == (
        None,
        None,
        None,
        None,
    )
    assert await backend.comments.get(row.id) == row


async def test_a_running_lease_keeps_other_holders_out(backend: ReservationBackend) -> None:
    first = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)

    during = await backend.comments.claim(
        COMMENT, holder="worker-2/2-0", now=NOW + LEASE / 2, lease=LEASE
    )
    # The lease runs until claimed_until inclusive: only a later claim may take it.
    at_the_end = await backend.comments.claim(
        COMMENT, holder="worker-2/2-0", now=NOW + LEASE, lease=LEASE
    )

    assert not during.acquired
    assert not at_the_end.acquired
    assert during.comment == first.comment


async def test_an_ended_lease_goes_to_the_next_holder(backend: ReservationBackend) -> None:
    first = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    later = NOW + LEASE + timedelta(seconds=1)

    claim = await backend.comments.claim(COMMENT, holder="worker-2/1-0", now=later, lease=LEASE)

    assert claim.acquired
    assert claim.comment.id == first.comment.id
    assert (claim.comment.claimed_by, claim.comment.claimed_until) == (
        "worker-2/1-0",
        later + LEASE,
    )


async def test_a_holder_claiming_again_takes_its_own_lease_again(
    backend: ReservationBackend,
) -> None:
    first = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    retry = NOW + timedelta(seconds=2)

    again = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=retry, lease=LEASE)

    assert again.acquired
    assert again.comment.id == first.comment.id
    assert again.comment.claimed_until == retry + LEASE


async def test_a_redelivery_keeps_the_first_text(backend: ReservationBackend) -> None:
    await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    edited = queued_comment("je prends C3", external_comment_id="comment-1", author="buyer-1")

    claim = await backend.comments.claim(
        edited, holder="worker-2/2-0", now=NOW + 2 * LEASE, lease=LEASE
    )

    assert claim.acquired
    assert claim.comment.text == "je prends A12 M"


async def test_one_comment_id_per_platform(backend: ReservationBackend) -> None:
    tiktok = queued_comment(external_comment_id="comment-1", platform=Platform.TIKTOK)

    first = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    other = await backend.comments.claim(tiktok, holder="worker-1/2-0", now=NOW, lease=LEASE)

    assert other.acquired
    assert other.comment.id != first.comment.id


async def test_twenty_parallel_deliveries_give_one_lease(backend: ReservationBackend) -> None:
    claims = await asyncio.gather(
        *(
            backend.comments.claim(COMMENT, holder=f"worker-{n}/{n}-0", now=NOW, lease=LEASE)
            for n in range(20)
        )
    )

    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.comment.id for claim in claims}) == 1


async def test_a_settled_comment_is_never_leased_again(backend: ReservationBackend) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    await backend.comments.reject(claim.comment.id, RejectionReason.NO_INTENT, session_id=None)

    again = await backend.comments.claim(
        COMMENT, holder="worker-1/1-0", now=NOW + 10 * LEASE, lease=LEASE
    )

    assert not again.acquired
    assert again.comment.status is CommentStatus.REJECTED


async def test_the_first_receipt_is_the_one_kept(backend: ReservationBackend) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    ok = FallbackReceipt(
        LlmOutcome.OK,
        LlmProposal("A12", "M", 2, Decimal("0.85")),
        request_id="req_1",
        model="claude-test",
        input_tokens=210,
        output_tokens=18,
        latency_ms=640,
    )

    stored = await backend.comments.record_llm_receipt(claim.comment.id, ok)
    again = await backend.comments.record_llm_receipt(
        claim.comment.id, FallbackReceipt(LlmOutcome.TIMEOUT)
    )

    assert stored == ok
    assert again == ok
    row = await backend.comments.get(claim.comment.id)
    assert row is not None
    assert row.llm_receipt == ok


async def test_a_receipt_without_a_proposal_is_kept_as_it_came(
    backend: ReservationBackend,
) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    busy = FallbackReceipt(LlmOutcome.LLM_BUSY)

    assert await backend.comments.record_llm_receipt(claim.comment.id, busy) == busy


async def test_a_rejection_settles_once_and_keeps_its_first_reason(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    ambiguous = ParseSummary(ParseKind.AMBIGUOUS, AmbiguityReason.UNKNOWN_CODE)

    first = await backend.comments.reject(
        claim.comment.id,
        RejectionReason.AMBIGUOUS_UNRESOLVED,
        session_id=session.id,
        parse=ambiguous,
    )
    second = await backend.comments.reject(
        claim.comment.id, RejectionReason.NO_INTENT, session_id=None
    )

    row = await backend.comments.get(claim.comment.id)
    assert (first, second) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert row is not None
    assert row.status is CommentStatus.REJECTED
    assert row.rejection_reason is RejectionReason.AMBIGUOUS_UNRESOLVED
    assert row.session_id == session.id
    assert row.parse == ambiguous


@pytest.mark.parametrize(
    "parse",
    [
        ParseSummary(ParseKind.REJECTED, RejectionReason.VARIANT_NOT_FOUND),
        None,
    ],
)
async def test_a_rejection_records_what_the_parser_said_if_it_ran(
    backend: ReservationBackend, parse: ParseSummary | None
) -> None:
    session = await backend.add_live_session()
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)

    await backend.comments.reject(
        claim.comment.id,
        RejectionReason.VARIANT_NOT_FOUND if parse else RejectionReason.SESSION_NOT_LIVE,
        session_id=session.id,
        parse=parse,
    )

    row = await backend.comments.get(claim.comment.id)
    assert row is not None
    assert row.parse == parse


async def test_a_dead_letter_settles_a_received_comment_once(backend: ReservationBackend) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)

    first = await backend.comments.dead_letter(COMMENT, now=NOW + LEASE)
    second = await backend.comments.dead_letter(COMMENT, now=NOW + 2 * LEASE)

    row = await backend.comments.get(claim.comment.id)
    assert (first, second) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert row is not None
    assert row.status is CommentStatus.DEAD_LETTERED
    assert row.created_at == NOW


async def test_a_comment_never_recorded_is_recorded_dead_lettered(
    backend: ReservationBackend,
) -> None:
    later = NOW + timedelta(minutes=6)

    outcome = await backend.comments.dead_letter(COMMENT, now=later)
    again = await backend.comments.claim(COMMENT, holder="worker-1/9-0", now=later, lease=LEASE)

    assert outcome is TransitionOutcome.APPLIED
    row = again.comment
    assert not again.acquired
    assert row.status is CommentStatus.DEAD_LETTERED
    assert (row.platform, row.external_comment_id) == (Platform.SIMULATOR, "comment-1")
    assert (row.author_platform_user_id, row.text) == ("buyer-1", "je prends A12 M")
    assert row.correlation_id == COMMENT.correlation_id
    assert (row.created_at, row.claimed_by, row.session_id) == (later, None, None)


async def test_a_settled_comment_takes_no_other_terminal_status(
    backend: ReservationBackend,
) -> None:
    claim = await backend.comments.claim(COMMENT, holder="worker-1/1-0", now=NOW, lease=LEASE)
    await backend.comments.dead_letter(COMMENT, now=NOW)

    with pytest.raises(InvalidTransition):
        await backend.comments.reject(claim.comment.id, RejectionReason.NO_INTENT, session_id=None)

    rejected = queued_comment(external_comment_id="comment-2")
    other = await backend.comments.claim(rejected, holder="worker-1/2-0", now=NOW, lease=LEASE)
    await backend.comments.reject(other.comment.id, RejectionReason.NO_INTENT, session_id=None)
    with pytest.raises(InvalidTransition):
        await backend.comments.dead_letter(rejected, now=NOW)


async def test_unknown_comments_are_not_found(backend: ReservationBackend) -> None:
    assert await backend.comments.get(UNKNOWN) is None
    with pytest.raises(RecordNotFound):
        await backend.comments.record_llm_receipt(UNKNOWN, FallbackReceipt(LlmOutcome.DISABLED))
    with pytest.raises(RecordNotFound):
        await backend.comments.reject(UNKNOWN, RejectionReason.NO_INTENT, session_id=None)


async def test_a_claim_needs_a_lease(backend: ReservationBackend) -> None:
    with pytest.raises(ValueError, match="lease"):
        await backend.comments.claim(COMMENT, holder="w", now=NOW, lease=timedelta(0))
