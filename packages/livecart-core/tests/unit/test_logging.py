import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog

from livecart_core.logging import REDACTED, configure_logging

ReadLines = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def read_logs() -> Iterator[ReadLines]:
    """Configure JSON logging into a buffer, then restore the previous logging state."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    uvicorn = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.access")]
    saved_uvicorn = [(lg, list(lg.handlers), lg.propagate) for lg in uvicorn]
    buffer = io.StringIO()
    configure_logging(service="test-service", level="INFO", stream=buffer)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buffer.getvalue().splitlines()]

    yield read

    structlog.reset_defaults()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for lg, handlers, propagate in saved_uvicorn:
        lg.handlers[:] = handlers
        lg.propagate = propagate


def test_structlog_event_is_one_json_object_per_line(read_logs: ReadLines) -> None:
    structlog.get_logger("livecart.test").info("order_created", order_id="o-1", quantity=2)

    [line] = read_logs()
    assert line["event"] == "order_created"
    assert line["level"] == "info"
    assert line["logger"] == "livecart.test"
    assert line["service"] == "test-service"
    assert line["order_id"] == "o-1"
    assert line["quantity"] == 2
    assert datetime.fromisoformat(line["timestamp"]).utcoffset() == UTC.utcoffset(None)


def test_trace_fields_are_always_present(read_logs: ReadLines) -> None:
    log = structlog.get_logger("livecart.test")

    log.info("outside_any_request")
    with structlog.contextvars.bound_contextvars(request_id="req-1", correlation_id="cor-1"):
        log.info("inside_a_request")
    log.info("after_the_request")

    outside, inside, after = read_logs()
    assert (outside["request_id"], outside["correlation_id"]) == (None, None)
    assert (inside["request_id"], inside["correlation_id"]) == ("req-1", "cor-1")
    assert (after["request_id"], after["correlation_id"]) == (None, None)


def test_uvicorn_handlers_give_way_to_the_json_handler(read_logs: ReadLines) -> None:
    # What uvicorn's default logging config installs before importing the app.
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler())
    access.propagate = False

    configure_logging(service="test-service", stream=io.StringIO())

    assert access.handlers == []
    assert access.propagate


def test_standard_library_records_are_rendered_with_context(read_logs: ReadLines) -> None:
    with structlog.contextvars.bound_contextvars(request_id="req-2"):
        logging.getLogger("uvicorn.access").info('%s - "%s" %d', "127.0.0.1", "GET /health", 200)

    [line] = read_logs()
    assert line["event"] == '127.0.0.1 - "GET /health" 200'
    assert line["logger"] == "uvicorn.access"
    assert line["level"] == "info"
    assert line["service"] == "test-service"
    assert line["request_id"] == "req-2"
    assert line["correlation_id"] is None


def test_line_breaks_cannot_forge_log_lines(read_logs: ReadLines) -> None:
    structlog.get_logger().info("comment_received", text='hi\n{"event": "forged"}\r\n')
    logging.getLogger("other").warning("multi\nline")

    first, second = read_logs()
    assert first["text"] == 'hi\n{"event": "forged"}\r\n'
    assert second["event"] == "multi\nline"


def test_sensitive_values_are_redacted(read_logs: ReadLines) -> None:
    structlog.get_logger().info(
        "login_attempt",
        password="hunter2",
        access_token="tok-123",
        Authorization="Bearer abc",
        api_key="sk-live",
        input_tokens=42,
        seller_id="s-1",
    )

    [line] = read_logs()
    assert line["password"] == REDACTED
    assert line["access_token"] == REDACTED
    assert line["Authorization"] == REDACTED
    assert line["api_key"] == REDACTED
    assert line["input_tokens"] == 42
    assert line["seller_id"] == "s-1"


def test_exceptions_are_rendered_inside_the_json_line(read_logs: ReadLines) -> None:
    log = structlog.get_logger()
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("processing_failed")
        logging.getLogger("stdlib").exception("stdlib_failed")

    structlog_line, stdlib_line = read_logs()
    for line in (structlog_line, stdlib_line):
        assert line["level"] == "error"
        assert "ValueError: boom" in line["exception"]


def test_level_threshold_applies_to_both_apis(read_logs: ReadLines) -> None:
    structlog.get_logger().debug("hidden")
    logging.getLogger("stdlib").debug("hidden too")
    structlog.get_logger().warning("shown")

    assert [line["event"] for line in read_logs()] == ["shown"]
