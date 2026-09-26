"""``livecart-simulate``: its options, settings and refusals, and a run against the real API app.

The run posts through the API's own FastAPI app, in process, with fakes
behind it: the webhook checks the signature, the platform and the batch as
it does in compose, and every comment the simulator posts reaches the queue.
"""

import asyncio
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from livecart_api.testing import current_time_clock, make_app
from livecart_core.testing.fakes import FakeCommentQueue
from livecart_core.testing.keys import TEST_COMMENT_WEBHOOK_SECRET
from livecart_simulator import main as cli
from livecart_simulator.settings import SimulatorSettings

SECRET = TEST_COMMENT_WEBHOOK_SECRET.get_secret_value()


def _keep_logging(*, service: str, level: str = "INFO", stream: object = None) -> None:
    """In place of ``configure_logging``: main() would reroute the whole test session's logs."""


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings from the environment only (no .env), and the session's logging kept."""
    monkeypatch.chdir(tmp_path)
    for name in ("APP_ENV", "REDIS_URL", "COMMENT_WEBHOOK_SECRET_SIMULATOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "configure_logging", _keep_logging)


async def test_a_run_posts_every_comment_through_the_real_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMENT_WEBHOOK_SECRET_SIMULATOR", SECRET)
    clock = current_time_clock()
    queue = FakeCommentQueue(clock)
    app = make_app(clock=clock, comment_queue=queue)
    args = cli.parse_args(["--count", "25", "--rate", "100", "--seed", "4"])
    transport = httpx2.ASGITransport(app=app)

    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        audience = cli.build_audience(cli.load_settings(args), client, seed=args.seed)
        summary = await asyncio.wait_for(audience.run(asyncio.Event(), count=args.count), 10)

    assert summary.posted == 25
    assert summary.refused == 0
    deliveries = await queue.receive("worker-1", max_messages=100, block_seconds=0)
    comments = [delivery.comment.comment for delivery in deliveries]
    assert len(comments) == 25
    assert {comment.external_live_id for comment in comments} == {"friday-drop"}


def test_the_options_override_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMULATOR_RATE", "3")
    monkeypatch.setenv("SIMULATOR_EXCLUDE_CODES", "b07, E15,")
    monkeypatch.setenv("COMMENT_WEBHOOK_SECRET_SIMULATOR", SECRET)

    from_environment = cli.load_settings(cli.parse_args([]))
    overridden = cli.load_settings(
        cli.parse_args(
            ["--rate", "0.5", "--exclude-codes", "Z99", "--buyers", "3", "--api-url", "http://x/"]
        )
    )

    assert from_environment.simulator_rate == 3.0
    assert from_environment.simulator_exclude_codes == ("B07", "E15")
    assert from_environment.simulator_api_url == "http://127.0.0.1:8000"
    assert overridden.simulator_rate == 0.5
    assert overridden.simulator_exclude_codes == ("Z99",)
    assert overridden.simulator_buyers == 3
    assert overridden.simulator_api_url == "http://x"


@pytest.mark.parametrize(
    "values",
    [
        {"simulator_rate": 0},
        {"simulator_buyers": 0},
        {"simulator_api_url": "ftp://api"},
        {"comment_webhook_secret_simulator": "short"},
        {"redis_url": "http://redis"},
        {"redis_key_prefix": "not a prefix"},
    ],
)
def test_settings_out_of_bounds_are_refused(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SimulatorSettings.model_validate(values)


def test_an_unknown_session_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--live-id", "not-seeded"])

    assert excinfo.value.code == 2
    assert "friday-drop" in capsys.readouterr().err


def test_production_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("COMMENT_WEBHOOK_SECRET_SIMULATOR", SECRET)

    assert cli.main(["--count", "1"]) == 1
    assert cli.main(["--text"]) == 1
    assert "refused when APP_ENV=production" in capsys.readouterr().err


def test_commenting_needs_the_webhook_secret(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--count", "1"]) == 1

    assert "COMMENT_WEBHOOK_SECRET_SIMULATOR must be set" in capsys.readouterr().err


def test_the_text_mode_needs_redis(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--text", "--no-follow"]) == 1

    assert "REDIS_URL must be set" in capsys.readouterr().err


def test_an_excluded_code_the_catalog_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_WEBHOOK_SECRET_SIMULATOR", SECRET)

    assert cli.main(["--exclude-codes", "X9", "--count", "1"]) == 1

    assert "does not have: X9" in capsys.readouterr().err


def test_invalid_settings_are_reported_without_their_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_WEBHOOK_SECRET_SIMULATOR", "short-secret-value")

    assert cli.main([]) == 1

    error = capsys.readouterr().err
    assert "invalid settings" in error
    assert "short-secret-value" not in error


async def test_the_live_id_names_a_seeded_session_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMULATOR_LIVE_ID", "not-seeded")
    settings = cli.load_settings(cli.parse_args([]))

    async with httpx2.AsyncClient() as client:
        with pytest.raises(cli.Refused, match="knows friday-drop, loadtest"):
            cli.build_audience(settings, client, seed=None)
