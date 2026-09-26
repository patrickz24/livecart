"""The worker service of compose.yaml gives a stop the time the consumer's drain may take."""

import re
from pathlib import Path
from typing import Final

COMPOSE: Final = Path(__file__).resolve().parents[4] / "compose.yaml"
# What the process needs after its drain: close its pools and its Redis client.
CLOSE_MARGIN_S: Final = 5


def _service_block(compose: str, service: str) -> str:
    """The lines of one service of compose.yaml, up to the next service."""
    match = re.search(rf"^  {service}:\n((?:    .*\n|\n)*)", compose, flags=re.MULTILINE)
    assert match is not None, f"no {service} service in compose.yaml"
    return match.group(1)


def _seconds(pattern: str, block: str) -> int:
    match = re.search(pattern, block, flags=re.MULTILINE)
    assert match is not None, f"no match for {pattern} in the worker service"
    return int(match.group(1))


def test_the_stop_grace_period_outlasts_one_message_deadline() -> None:
    block = _service_block(COMPOSE.read_text(encoding="utf-8"), "worker")

    grace_s = _seconds(r"^    stop_grace_period: (\d+)s$", block)
    deadline_s = _seconds(
        r"^      MESSAGE_PROCESS_TIMEOUT_S: \$\{MESSAGE_PROCESS_TIMEOUT_S:-(\d+)\}$", block
    )

    # On a stop, only the handlers that started run on, each within its
    # deadline: the drain takes one deadline at most, whatever WORKER_BATCH.
    assert grace_s >= deadline_s + CLOSE_MARGIN_S
