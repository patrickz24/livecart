"""A streaming client of the app's Server-Sent Events, for the tests: frame by frame, as they come.

``httpx2.ASGITransport`` collects a whole response before it returns it, so
a stream that stays open would never return. ``open_event_stream`` calls the
ASGI app itself instead: it hands the request over, then reads each body
chunk as the app sends it, parses the SSE frames (``event:``, ``id:``,
``data:``, ``retry:`` and ``:`` comments), and disconnects when the block
ends, as a browser closing its tab would. Only test code imports it.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Final

from starlette.types import ASGIApp, Message

# How long a test waits for the next frame before it fails.
FRAME_TIMEOUT_S: Final = 5.0


class StreamEnded(Exception):
    """The app ended the response: no frame will come."""


@dataclass(frozen=True, slots=True)
class SseFrame:
    """One frame of the stream: a message, or a comment alone (the keep-alive ``: ping``)."""

    event: str | None = None
    id: str | None = None
    data: str | None = None
    retry: int | None = None
    comments: tuple[str, ...] = ()

    @property
    def is_message(self) -> bool:
        return self.data is not None or self.event is not None

    def json(self) -> Any:
        if self.data is None:
            raise ValueError("this frame carries no data")
        return json.loads(self.data)


def parse_frame(text: str) -> SseFrame:
    """The fields of one frame, the text between two blank lines."""
    values: dict[str, list[str]] = {}
    comments: list[str] = []
    for line in text.split("\n"):
        if line.startswith(":"):
            comments.append(line[1:].strip())
            continue
        name, _, value = line.partition(":")
        values.setdefault(name, []).append(value.removeprefix(" "))
    retry = values.get("retry")
    return SseFrame(
        event=values["event"][-1] if "event" in values else None,
        id=values["id"][-1] if "id" in values else None,
        data="\n".join(values["data"]) if "data" in values else None,
        retry=int(retry[-1]) if retry else None,
        comments=tuple(comments),
    )


@dataclass
class EventStream:
    """An open response: its status and headers, then its frames one by one."""

    status: int
    headers: Mapping[str, str]
    _messages: "asyncio.Queue[Message]"
    _buffer: str = ""
    _ended: bool = False
    frames: list[SseFrame] = field(default_factory=list)

    async def next_frame(self, timeout_s: float = FRAME_TIMEOUT_S) -> SseFrame:
        """The next frame, comments included; ``StreamEnded`` once the app ended the response."""
        async with asyncio.timeout(timeout_s):
            while "\n\n" not in self._buffer:
                if self._ended:
                    raise StreamEnded
                message = await self._messages.get()
                if message["type"] == "http.response.body":
                    self._buffer += message.get("body", b"").decode()
                    self._ended = not message.get("more_body", False)
        text, _, self._buffer = self._buffer.partition("\n\n")
        frame = parse_frame(text)
        self.frames.append(frame)
        return frame

    async def next_message(self, timeout_s: float = FRAME_TIMEOUT_S) -> SseFrame:
        """The next frame that is a message, the keep-alive comments skipped."""
        async with asyncio.timeout(timeout_s):
            while not (frame := await self.next_frame(timeout_s)).is_message:
                pass
        return frame

    async def rest(self, timeout_s: float = FRAME_TIMEOUT_S) -> list[SseFrame]:
        """Every message until the app ends the response."""
        messages = []
        with suppress(StreamEnded):
            while True:
                messages.append(await self.next_message(timeout_s))
        return messages

    async def body(self, timeout_s: float = FRAME_TIMEOUT_S) -> str:
        """The whole body of a response that is no stream, such as a 404."""
        async with asyncio.timeout(timeout_s):
            while not self._ended:
                message = await self._messages.get()
                if message["type"] == "http.response.body":
                    self._buffer += message.get("body", b"").decode()
                    self._ended = not message.get("more_body", False)
        return self._buffer


@asynccontextmanager
async def open_event_stream(
    app: ASGIApp,
    path: str,
    headers: Mapping[str, str],
    *,
    host: str = "test",
    peer: str = "127.0.0.1",
) -> AsyncIterator[EventStream]:
    """GET ``path`` from ``app``, and disconnect when the block ends; see the module docstring."""
    messages: asyncio.Queue[Message] = asyncio.Queue()
    disconnected = asyncio.Event()
    requested = False

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await messages.put(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", host.encode()),
            *((name.lower().encode(), value.encode()) for name, value in headers.items()),
        ],
        "client": (peer, 50_000),
        "server": (host, 80),
    }

    async def respond() -> None:
        await app(scope, receive, send)

    task = asyncio.create_task(respond())
    try:
        async with asyncio.timeout(FRAME_TIMEOUT_S):
            start = await messages.get()
        if start["type"] != "http.response.start":
            raise RuntimeError(f"the app sent {start['type']} before its response started")
        response_headers = {
            name.decode().lower(): value.decode() for name, value in start.get("headers", [])
        }
        yield EventStream(start["status"], response_headers, messages)
    finally:
        disconnected.set()
        async with asyncio.timeout(FRAME_TIMEOUT_S):
            await task
