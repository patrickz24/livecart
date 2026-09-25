import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import FallbackReceipt, Platform
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.outcomes import LlmOutcome, RejectionReason
from livecart_core.domain.state_machines import CommentStatus, TransitionOutcome
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryCommentInbox, InMemoryDatabase

NOW = FAKE_CLOCK_START
LEASE = timedelta(seconds=30)
COMMENT = queued_comment("je prends A12 M", external_comment_id="comment-1", author="buyer-1")


@pytest.fixture
def inbox() -> InMemoryCommentInbox:
    return InMemoryDatabase().comments


async def test_a_new_comment_is_recorded_with_its_lease(inbox: InMemoryCommentInbox) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)

    row = claim.comment
    assert claim.acquired
    assert row.status is CommentStatus.RECEIVED
    assert (row.platform, row.external_comment_id) == (Platform.SIMULATOR, "comment-1")
    assert row.author_platform_user_id == "buyer-1"
    assert row.text == "je prends A12 M"
    assert row.correlation_id == COMMENT.correlation_id
    assert (row.claimed_by, row.claimed_until) == ("worker-1", NOW + LEASE)
    assert row.created_at == NOW
    assert row.session_id is None
    assert await inbox.get(row.id) == row


async def test_a_running_lease_keeps_other_deliveries_out(inbox: InMemoryCommentInbox) -> None:
    first = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)

    during = await inbox.claim(COMMENT, consumer="worker-2", now=NOW + LEASE / 2, lease=LEASE)
    # The lease runs until claimed_until inclusive: only a later claim may take it.
    at_the_end = await inbox.claim(COMMENT, consumer="worker-2", now=NOW + LEASE, lease=LEASE)

    assert not during.acquired
    assert not at_the_end.acquired
    assert during.comment == first.comment


async def test_an_ended_lease_goes_to_the_next_delivery(inbox: InMemoryCommentInbox) -> None:
    first = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    later = NOW + LEASE + timedelta(seconds=1)

    claim = await inbox.claim(COMMENT, consumer="worker-2", now=later, lease=LEASE)

    assert claim.acquired
    assert claim.comment.id == first.comment.id
    assert (claim.comment.claimed_by, claim.comment.claimed_until) == ("worker-2", later + LEASE)


async def test_a_redelivery_keeps_the_first_text(inbox: InMemoryCommentInbox) -> None:
    await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    edited = queued_comment("je prends C3", external_comment_id="comment-1", author="buyer-1")

    claim = await inbox.claim(edited, consumer="worker-2", now=NOW + 2 * LEASE, lease=LEASE)

    assert claim.comment.text == "je prends A12 M"


async def test_one_comment_id_per_platform(inbox: InMemoryCommentInbox) -> None:
    tiktok = queued_comment(external_comment_id="comment-1", platform=Platform.TIKTOK)

    first = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    other = await inbox.claim(tiktok, consumer="worker-1", now=NOW, lease=LEASE)

    assert other.acquired
    assert other.comment.id != first.comment.id


async def test_twenty_parallel_deliveries_give_one_lease(inbox: InMemoryCommentInbox) -> None:
    claims = await asyncio.gather(
        *(inbox.claim(COMMENT, consumer=f"worker-{n}", now=NOW, lease=LEASE) for n in range(20))
    )

    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.comment.id for claim in claims}) == 1


async def test_a_settled_comment_is_never_leased_again(inbox: InMemoryCommentInbox) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    await inbox.reject(claim.comment.id, RejectionReason.NO_INTENT, session_id=None)

    again = await inbox.claim(COMMENT, consumer="worker-2", now=NOW + 10 * LEASE, lease=LEASE)

    assert not again.acquired
    assert again.comment.status is CommentStatus.REJECTED


async def test_the_first_receipt_is_the_one_kept(inbox: InMemoryCommentInbox) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    busy = FallbackReceipt(LlmOutcome.LLM_BUSY)
    timeout = FallbackReceipt(LlmOutcome.TIMEOUT)

    stored = await inbox.record_llm_receipt(claim.comment.id, busy)
    again = await inbox.record_llm_receipt(claim.comment.id, timeout)

    assert stored is busy
    assert again is busy
    row = await inbox.get(claim.comment.id)
    assert row is not None
    assert row.llm_receipt is busy


async def test_a_rejection_settles_once_and_keeps_its_first_reason(
    inbox: InMemoryCommentInbox,
) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    session_id = UUID(int=99)

    first = await inbox.reject(
        claim.comment.id, RejectionReason.SESSION_NOT_LIVE, session_id=session_id
    )
    second = await inbox.reject(claim.comment.id, RejectionReason.NO_INTENT, session_id=None)

    row = await inbox.get(claim.comment.id)
    assert (first, second) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert row is not None
    assert row.status is CommentStatus.REJECTED
    assert row.rejection_reason is RejectionReason.SESSION_NOT_LIVE
    assert row.session_id == session_id


async def test_a_dead_letter_settles_a_received_comment_once(inbox: InMemoryCommentInbox) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)

    first = await inbox.dead_letter(Platform.SIMULATOR, "comment-1")
    second = await inbox.dead_letter(Platform.SIMULATOR, "comment-1")

    row = await inbox.get(claim.comment.id)
    assert (first, second) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert row is not None
    assert row.status is CommentStatus.DEAD_LETTERED


async def test_a_settled_comment_takes_no_other_terminal_status(
    inbox: InMemoryCommentInbox,
) -> None:
    claim = await inbox.claim(COMMENT, consumer="worker-1", now=NOW, lease=LEASE)
    await inbox.dead_letter(Platform.SIMULATOR, "comment-1")

    with pytest.raises(InvalidTransition):
        await inbox.reject(claim.comment.id, RejectionReason.NO_INTENT, session_id=None)

    rejected = queued_comment(external_comment_id="comment-2")
    other = await inbox.claim(rejected, consumer="worker-1", now=NOW, lease=LEASE)
    await inbox.reject(other.comment.id, RejectionReason.NO_INTENT, session_id=None)
    with pytest.raises(InvalidTransition):
        await inbox.dead_letter(Platform.SIMULATOR, "comment-2")


async def test_unknown_comments_are_not_found(inbox: InMemoryCommentInbox) -> None:
    unknown = UUID(int=404)

    assert await inbox.get(unknown) is None
    with pytest.raises(RecordNotFound):
        await inbox.record_llm_receipt(unknown, FallbackReceipt(LlmOutcome.DISABLED))
    with pytest.raises(RecordNotFound):
        await inbox.reject(unknown, RejectionReason.NO_INTENT, session_id=None)
    with pytest.raises(RecordNotFound):
        await inbox.dead_letter(Platform.SIMULATOR, "comment-404")


async def test_a_claim_needs_an_aware_time_and_a_lease(inbox: InMemoryCommentInbox) -> None:
    with pytest.raises(ValueError, match="naive"):
        await inbox.claim(COMMENT, consumer="w", now=NOW.replace(tzinfo=None), lease=LEASE)
    with pytest.raises(ValueError, match="lease"):
        await inbox.claim(COMMENT, consumer="w", now=NOW, lease=timedelta(0))
