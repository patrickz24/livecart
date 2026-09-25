"""Structured JSON logging shared by every LiveCart process.

structlog renders one JSON object per line on stdout, both for structlog loggers
and for standard-library loggers (uvicorn, SQLAlchemy, asyncio), so a log
collector only ever parses one format. JSON encoding escapes CR and LF, so a
value coming from a request cannot forge an extra log line.

Every line carries ``request_id`` and ``correlation_id``. They are null until
the code handling a request or a queued message binds them with
``structlog.contextvars.bound_contextvars``.
"""

import logging
import sys
from typing import TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

TRACE_FIELDS = ("request_id", "correlation_id")
REDACTED = "[redacted]"

# A key whose name ends with one of these never has its value logged.
_SENSITIVE_KEY_SUFFIXES = ("password", "secret", "token", "authorization", "cookie", "api_key")

# uvicorn installs its own handlers before it imports the application.
_STDLIB_LOGGERS_TO_PROPAGATE = ("uvicorn", "uvicorn.error", "uvicorn.access")


def add_trace_fields(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Give every line the same trace keys, null when nothing is bound."""
    for field in TRACE_FIELDS:
        event_dict.setdefault(field, None)
    return event_dict


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask top-level values whose key names a credential (``password``, ``*_token``...)."""
    for key in event_dict:
        if key.lower().replace("-", "_").endswith(_SENSITIVE_KEY_SUFFIXES):
            event_dict[key] = REDACTED
    return event_dict


def drop_color_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Drop the ANSI-colored duplicate of the message that uvicorn passes as ``extra``."""
    event_dict.pop("color_message", None)
    return event_dict


def _service_adder(service: str) -> Processor:
    def add_service(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def configure_logging(*, service: str, level: str = "INFO", stream: TextIO | None = None) -> None:
    """Route structlog and standard-library logging to one JSON handler.

    Called once by each process entry point, never at import time by library code.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _service_adder(service),
        add_trace_fields,
        redact_sensitive,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), drop_color_message, *shared_processors],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Plain traceback text: never the frame locals, which may hold secrets.
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _STDLIB_LOGGERS_TO_PROPAGATE:
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers.clear()
        stdlib_logger.propagate = True
