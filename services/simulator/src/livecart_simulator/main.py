"""``livecart-simulate``: a live audience commenting on a seeded session, or the buyers' messages.

Two modes:

- by default, comments arrive as a Poisson process at ``--rate`` a second on
  the seeded session ``--live-id`` (friday-drop), from ``--buyers`` fake
  buyers, and are posted to the signed webhook (``live.LiveAudience``), until
  Ctrl+C or ``--count`` comments. ``--exclude-codes B07,E15`` keeps products
  out of the comments, to buy them by hand meanwhile;
- ``--text`` prints the messages the buyers received, with each order's
  reference, token and link (``buyer_messages``), then follows them.

``docker compose up`` runs the first mode at a gentle rate; ``docker compose
run --rm simulator --text`` the second. The simulator refuses to run when
``APP_ENV=production``: it would post fake comments, and fake orders with
them, to a real live. Exit status: 0 once stopped or done, 1 when refused
or misconfigured, 2 for a usage error.
"""

import argparse
import asyncio
import logging
import random
import signal
import sys
from collections.abc import Callable, Sequence
from typing import Final

import httpx2
import structlog
from pydantic import ValidationError

from livecart_core.application.demo_seed import DEMO_SELLERS, DemoSession
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.logging import configure_logging
from livecart_simulator.buyer_messages import BuyerMessages
from livecart_simulator.comments import CommentWriter, buyer_ids, catalog_of
from livecart_simulator.live import LiveAudience
from livecart_simulator.poster import REQUEST_TIMEOUT_S, WebhookPoster
from livecart_simulator.settings import SimulatorSettings

logger = structlog.get_logger(__name__)

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)
# The seeded sessions the simulator knows the catalog of, by live id.
DEMO_SESSIONS: Final[dict[str, DemoSession]] = {
    session.external_live_id: session for seller in DEMO_SELLERS for session in seller.sessions
}


class Refused(Exception):
    """The simulator cannot run as configured; the message says why."""


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="livecart-simulate",
        description=(
            "Post simulated live comments to the LiveCart comment webhook, "
            "or print the messages the buyers received (--text)."
        ),
    )
    parser.add_argument(
        "--text", action="store_true", help="print the buyers' messages instead of commenting"
    )
    parser.add_argument(
        "--no-follow", action="store_true", help="with --text: stop at the last message"
    )
    parser.add_argument("--rate", type=float, help="comments per second (SIMULATOR_RATE, 0.2)")
    parser.add_argument("--count", type=int, help="stop after this many comments")
    parser.add_argument(
        "--live-id",
        choices=sorted(DEMO_SESSIONS),
        help="the seeded session to comment on (SIMULATOR_LIVE_ID, friday-drop)",
    )
    parser.add_argument(
        "--exclude-codes",
        help="product codes never named, between commas (SIMULATOR_EXCLUDE_CODES)",
    )
    parser.add_argument("--buyers", type=int, help="fake buyers (SIMULATOR_BUYERS, 12)")
    parser.add_argument("--api-url", help="the API's base URL (SIMULATOR_API_URL)")
    parser.add_argument("--seed", type=int, help="seed the random draws, to repeat a run")
    return parser.parse_args(argv)


def load_settings(args: argparse.Namespace) -> SimulatorSettings:
    """The settings, with every option given on the command line in place of its variable."""
    overrides = {
        "simulator_rate": args.rate,
        "simulator_live_id": args.live_id,
        "simulator_exclude_codes": args.exclude_codes,
        "simulator_buyers": args.buyers,
        "simulator_api_url": args.api_url,
    }
    return SimulatorSettings(
        **{key: value for key, value in overrides.items() if value is not None}
    )


def build_audience(
    settings: SimulatorSettings, client: httpx2.AsyncClient, *, seed: int | None
) -> LiveAudience:
    """The audience of the configured session, posting through ``client``."""
    session = DEMO_SESSIONS.get(settings.simulator_live_id)
    if session is None:
        raise Refused(
            f"no seeded session has the live id {settings.simulator_live_id!r}: "
            f"the simulator knows {', '.join(sorted(DEMO_SESSIONS))}"
        )
    secret = settings.comment_webhook_secret_simulator
    if secret is None:
        raise Refused("COMMENT_WEBHOOK_SECRET_SIMULATOR must be set: it signs the webhook calls")
    # Draws of a simulation, repeatable with --seed: nothing here is a secret.
    rng = random.Random(seed)  # noqa: S311
    try:
        writer = CommentWriter(
            catalog_of(session),
            buyers=buyer_ids(settings.simulator_buyers),
            rng=rng,
            exclude_codes=settings.simulator_exclude_codes,
        )
    except ValueError as error:
        raise Refused(str(error)) from None
    return LiveAudience(
        writer,
        WebhookPoster(client, secret),
        external_live_id=session.external_live_id,
        rate=settings.simulator_rate,
        rng=rng,
    )


async def comment(
    settings: SimulatorSettings, stop: asyncio.Event, args: argparse.Namespace
) -> None:
    async with httpx2.AsyncClient(
        base_url=settings.simulator_api_url, timeout=REQUEST_TIMEOUT_S
    ) as client:
        audience = build_audience(settings, client, seed=args.seed)
        await audience.run(stop, count=args.count)


async def show_messages(
    settings: SimulatorSettings, stop: asyncio.Event, args: argparse.Namespace
) -> None:
    if settings.redis_url is None:
        raise Refused("REDIS_URL must be set: --text reads the buyers' messages from Redis")
    client = create_redis_client(settings.redis_url)
    try:
        messages = BuyerMessages(client, prefix=settings.redis_key_prefix)
        await messages.print_all(sys.stdout, stop=stop, follow=not args.no_follow)
    finally:
        await client.aclose()


def install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> Callable[[], None]:
    """SIGTERM (docker stop) and SIGINT (Ctrl+C) set ``stop``; return what restores them."""
    try:
        for sig in STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        # Windows event loops lack add_signal_handler; asyncio still wakes up on signals.
        def on_signal(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(stop.set)

        previous = {sig: signal.signal(sig, on_signal) for sig in STOP_SIGNALS}

        def restore() -> None:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

        return restore

    def remove() -> None:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)

    return remove


async def run(settings: SimulatorSettings, args: argparse.Namespace) -> None:
    if settings.is_production:
        raise Refused(
            "the simulator posts fake comments, and opens fake orders: it is refused when "
            "APP_ENV=production"
        )
    stop = asyncio.Event()
    restore = install_stop_handlers(asyncio.get_running_loop(), stop)
    try:
        await (show_messages if args.text else comment)(settings, stop, args)
    finally:
        restore()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args)
    except ValidationError as error:
        # hide_input_in_errors keeps the secret out of the message.
        sys.stderr.write(f"livecart-simulate: invalid settings\n{error}\n")
        return 1
    # Standard error, like the other commands: the text mode prints on standard output.
    configure_logging(service="simulator", level=settings.log_level, stream=sys.stderr)
    # The poster logs each batch and each refusal: httpx2's line per request adds nothing.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        asyncio.run(run(settings, args))
    except Refused as error:
        sys.stderr.write(f"livecart-simulate: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
