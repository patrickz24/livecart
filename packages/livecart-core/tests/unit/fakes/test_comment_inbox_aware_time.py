"""What only the in-memory inbox promises; tests/contract holds what the SQL adapter shares."""

from datetime import timedelta

import pytest

from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase


async def test_a_claim_needs_an_aware_time() -> None:
    inbox = InMemoryDatabase().comments

    with pytest.raises(ValueError, match="naive"):
        await inbox.claim(
            queued_comment(),
            holder="w",
            now=FAKE_CLOCK_START.replace(tzinfo=None),
            lease=timedelta(seconds=30),
        )
