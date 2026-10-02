import type { LiveSessionSnapshot } from "@livecart/api-client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { aSession } from "../test/fixtures";
import { openLiveStream, type SessionEvent, type StreamStatus } from "./sseClient";

const URL_EVENTS = "http://localhost/api/v1/live-sessions/s1/events";

/** One connection of the fake server: what it was asked, and a body the test writes. */
interface Connection {
  request: Request;
  at: number;
  push(text: string): void;
  end(): void;
}

type Answer = number | "network-error" | { status: number; headers: Record<string, string> };

/**
 * The server side of the stream. Each request gets the next scripted answer:
 * a status code, a network error, or by default a 200 whose body the test
 * writes; an aborted request errors its body, as `fetch` does.
 */
class FakeServer {
  readonly connections: Connection[] = [];
  readonly attempts: { request: Request; at: number }[] = [];
  readonly #script: Answer[] = [];

  answer(...answers: Answer[]): this {
    this.#script.push(...answers);
    return this;
  }

  send = (request: Request): Promise<Response> => {
    this.attempts.push({ request, at: Date.now() });
    const scripted = this.#script.shift();
    if (scripted === "network-error") {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    if (typeof scripted === "number") {
      return Promise.resolve(Response.json({ detail: "refused" }, { status: scripted }));
    }
    if (scripted !== undefined) {
      return Promise.resolve(Response.json({ detail: "refused" }, scripted));
    }
    const encoder = new TextEncoder();
    let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(streamController) {
        controller = streamController;
      },
    });
    request.signal.addEventListener("abort", () => {
      controller?.error(request.signal.reason);
    });
    this.connections.push({
      request,
      at: Date.now(),
      push: (text) => controller?.enqueue(encoder.encode(text)),
      end: () => controller?.close(),
    });
    return Promise.resolve(
      new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    );
  };

  get last(): Connection {
    const connection = this.connections.at(-1);
    if (connection === undefined) {
      throw new Error("no connection yet");
    }
    return connection;
  }
}

function snapshot(cursor: string): LiveSessionSnapshot {
  return {
    session: aSession({ status: "live" }),
    events_cursor: cursor,
    products: [],
    intents: [],
    rejections: [],
    orders: [],
    limit: 200,
    taken_at: "2026-10-02T10:00:00Z",
  };
}

function stockEvent(available: number): SessionEvent {
  return {
    type: "stock.updated",
    session_id: "s1",
    occurred_at: "2026-10-02T10:00:01Z",
    correlation_id: null,
    data: { product_id: "p1", stock_total: 10, reserved: 10 - available, sold: 0, available },
  };
}

const message = (event: string, data: unknown, id?: string) =>
  `${id === undefined ? "" : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

const closeMessage = (reason: string) =>
  `event: stream.close\ndata: ${JSON.stringify({ type: "stream.close", session_id: "s1", occurred_at: "2026-10-02T10:14:55Z", correlation_id: null, data: { reason } })}\nretry: 1000\n\n`;

const gapMessage = () =>
  message("stream.gap", {
    type: "stream.gap",
    session_id: "s1",
    occurred_at: "2026-10-02T10:00:00Z",
    correlation_id: null,
    data: { after: "1-0" },
  });

/** Let the client's promises settle without moving the clock. */
const settle = () => vi.advanceTimersByTimeAsync(0);

function open(
  server: Pick<FakeServer, "send">,
  tokens: (string | null)[] = ["t1"],
  random = () => 0,
) {
  const received: { snapshots: string[]; events: SessionEvent[]; gaps: number } = {
    snapshots: [],
    events: [],
    gaps: 0,
  };
  const statuses: StreamStatus[] = [];
  const auth = {
    current: tokens[0] ?? null,
    accessToken: vi.fn((): string | null => auth.current),
    refresh: vi.fn((): Promise<string | null> => {
      tokens.shift();
      auth.current = tokens[0] ?? null;
      return Promise.resolve(auth.current);
    }),
  };
  const stream = openLiveStream({
    url: URL_EVENTS,
    send: server.send,
    auth,
    random,
    handlers: {
      onSnapshot: (value) => received.snapshots.push(value.events_cursor),
      onEvent: (event) => received.events.push(event),
      onGap: () => {
        received.gaps += 1;
      },
      onStatus: (status) => statuses.push(status),
    },
  });
  return { stream, received, statuses, auth };
}

beforeEach(() => {
  vi.useFakeTimers({ now: new Date("2026-10-02T10:00:00Z") });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("openLiveStream", () => {
  it("sends the token as a bearer, then hands over the snapshot and the events", async () => {
    const server = new FakeServer();
    const { received, statuses, stream } = open(server);
    await settle();

    const { request } = server.last;
    expect(request.url).toBe(URL_EVENTS);
    expect(request.headers.get("Authorization")).toBe("Bearer t1");
    expect(request.headers.get("Accept")).toBe("text/event-stream");
    expect(request.headers.get("Last-Event-ID")).toBeNull();

    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    server.last.push(message("stock.updated", stockEvent(9), "2-0").slice(0, 20));
    await settle();
    server.last.push(message("stock.updated", stockEvent(9), "2-0").slice(20));
    await settle();

    expect(received.snapshots).toEqual(["1-0"]);
    expect(received.events.map((event) => event.type)).toEqual(["stock.updated"]);
    expect(statuses).toEqual(["connecting", "open"]);
    stream.close();
  });

  it("resumes with Last-Event-ID one second after the connection ends", async () => {
    const server = new FakeServer();
    const { received, stream } = open(server);
    await settle();
    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    server.last.push(message("stock.updated", stockEvent(9), "2-0"));
    await settle();

    const droppedAt = Date.now();
    server.last.end();
    await settle();
    await vi.advanceTimersByTimeAsync(999);
    expect(server.connections).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);

    expect(server.connections).toHaveLength(2);
    expect(server.last.at - droppedAt).toBe(1000);
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("2-0");
    server.last.push(message("stock.updated", stockEvent(8), "3-0"));
    await settle();
    expect(received.snapshots).toEqual(["1-0"]);
    expect(received.events).toHaveLength(2);
    stream.close();
  });

  it("backs off exponentially from 1 s to 30 s while the API cannot be reached", async () => {
    const server = new FakeServer().answer(
      "network-error",
      502,
      "network-error",
      502,
      "network-error",
      502,
      "network-error",
    );
    const { stream } = open(server);
    await settle();

    await vi.advanceTimersByTimeAsync(91_000);

    const gaps = server.attempts
      .slice(1)
      .map((attempt, index) => attempt.at - (server.attempts[index]?.at ?? 0));
    expect(gaps).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    // The eighth attempt got the default answer: connected again.
    expect(server.connections).toHaveLength(1);
    stream.close();
  });

  it("keeps every wait within 1 s and 30 s, whatever the jitter", async () => {
    const server = new FakeServer().answer(...Array<Answer>(9).fill("network-error"));
    const { stream } = open(server, ["t1"], () => 0.999);
    await settle();

    await vi.advanceTimersByTimeAsync(200_000);

    const waits = server.attempts
      .slice(1, 10)
      .map((attempt, index) => attempt.at - (server.attempts[index]?.at ?? 0));
    expect(waits).toHaveLength(9);
    expect(waits.every((wait) => wait >= 1000 && wait <= 30_000)).toBe(true);
    expect(waits[0]).toBe(1999);
    stream.close();
  });

  it("waits at least the Retry-After of a 503", async () => {
    const server = new FakeServer().answer({ status: 503, headers: { "Retry-After": "5" } });
    const { stream } = open(server);
    await settle();

    await vi.advanceTimersByTimeAsync(4999);
    expect(server.attempts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(server.attempts).toHaveLength(2);
    stream.close();
  });

  it("refreshes the token on a 401 and reconnects with it at once", async () => {
    const server = new FakeServer().answer(401);
    const { auth, stream } = open(server, ["t1", "t2"]);
    await settle();

    expect(auth.refresh).toHaveBeenCalledTimes(1);
    expect(server.attempts).toHaveLength(2);
    expect(server.last.request.headers.get("Authorization")).toBe("Bearer t2");
    expect(server.last.at).toBe(server.attempts[0]?.at);
    stream.close();
  });

  it("stops when the refresh is refused, or the session is not the seller's", async () => {
    const refused = open(new FakeServer().answer(401), ["t1"]);
    const missing = open(new FakeServer().answer(404));
    await settle();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(refused.statuses.at(-1)).toBe("signed_out");
    expect(missing.statuses.at(-1)).toBe("not_found");
  });

  it("lets go of a refused answer without waiting for its body", async () => {
    // A body whose cancel never settles, as a tee'd or stalled one can be.
    const stalled = new ReadableStream<Uint8Array>({
      cancel: () => new Promise<void>(() => undefined),
    });
    const { statuses } = open({
      send: () => Promise.resolve(new Response(stalled, { status: 404 })),
    });
    await settle();

    expect(statuses.at(-1)).toBe("not_found");
  });

  it("forgets its cursor on stream.gap and hands over the snapshot that follows", async () => {
    const server = new FakeServer();
    const { received, stream } = open(server);
    await settle();
    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    await settle();
    server.last.end();
    await vi.advanceTimersByTimeAsync(1000);

    // The cursor is past the stream's retention: a gap, then a new snapshot.
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("1-0");
    server.last.push(gapMessage());
    await settle();
    expect(received.gaps).toBe(1);

    // Dropped before the snapshot: the next connection asks for one, without a cursor.
    server.last.end();
    await vi.advanceTimersByTimeAsync(1000);
    expect(server.last.request.headers.get("Last-Event-ID")).toBeNull();
    server.last.push(message("snapshot", snapshot("9-0"), "9-0"));
    await settle();

    expect(received.snapshots).toEqual(["1-0", "9-0"]);
    server.last.end();
    await vi.advanceTimersByTimeAsync(1000);
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("9-0");
    stream.close();
  });

  it("reconnects after a planned close, refreshing a token about to expire", async () => {
    const server = new FakeServer();
    const { auth, stream } = open(server, ["t1", "t2"]);
    await settle();
    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    await settle();

    auth.current = null; // within its expiry margin
    server.last.push(closeMessage("token_expiring"));
    await settle();
    await vi.advanceTimersByTimeAsync(1000);

    expect(auth.refresh).toHaveBeenCalledTimes(1);
    expect(server.connections).toHaveLength(2);
    expect(server.last.request.headers.get("Authorization")).toBe("Bearer t2");
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("1-0");
    stream.close();
  });

  it("aborts a connection silent for 45 s, while the 15 s pings keep one alive", async () => {
    const server = new FakeServer();
    const { stream } = open(server);
    await settle();
    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    await settle();
    const first = server.last;

    for (let ping = 0; ping < 4; ping += 1) {
      await vi.advanceTimersByTimeAsync(15_000);
      first.push(": ping\n\n");
      await settle();
    }
    expect(first.request.signal.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(45_000);
    expect(first.request.signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);

    expect(server.connections).toHaveLength(2);
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("1-0");
    stream.close();
  });

  it("reports an outage only once it lasts 5 s, and the stream open again after it", async () => {
    const server = new FakeServer();
    const { statuses, stream } = open(server);
    await settle();
    server.last.push(message("snapshot", snapshot("1-0"), "1-0"));
    await settle();

    // A planned reconnection within a second shows no banner.
    server.last.push(closeMessage("max_duration"));
    await vi.advanceTimersByTimeAsync(1000);
    expect(statuses).toEqual(["connecting", "open", "connecting", "open"]);

    // An API restart: the stream ends, the next attempts fail for a while.
    server.answer(502, 502, 502);
    server.last.end();
    await vi.advanceTimersByTimeAsync(4999);
    expect(statuses.at(-1)).toBe("connecting");
    await vi.advanceTimersByTimeAsync(1);
    expect(statuses.at(-1)).toBe("interrupted");

    await vi.advanceTimersByTimeAsync(30_000);
    expect(statuses.at(-1)).toBe("open");
    expect(server.last.request.headers.get("Last-Event-ID")).toBe("1-0");
    stream.close();
  });

  it("aborts its connection and stops on close", async () => {
    const server = new FakeServer();
    const { stream, statuses } = open(server);
    await settle();

    stream.close();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(server.last.request.signal.aborted).toBe(true);
    expect(server.connections).toHaveLength(1);
    expect(statuses.at(-1)).toBe("closed");
  });
});
