/**
 * The live stream of a session's events, read with `fetch` and
 * eventsource-parser rather than `EventSource`, which cannot send an
 * `Authorization` header (ADR 0009, ADR 0017).
 *
 * One connection at a time, opened again until `close()`:
 *
 * - each request carries the access token as a bearer, and `Last-Event-ID`
 *   once a message with an id has arrived: the API then resumes after it
 *   without a snapshot (within the 15 minutes its stream keeps);
 * - a dropped connection, a network error, a 5xx (nginx's 502 while the API
 *   restarts) or a stream that ends by itself is tried again after an
 *   exponential backoff: 1 s, 2 s, 4 s... up to 30 s, plus up to 1 s of
 *   jitter so that dashboards do not all come back at once, and at least
 *   the `Retry-After` of a 429 or a 503;
 * - a 401 refreshes the token (the session's single-flight refresh) and
 *   reconnects at once; a refused refresh, or a 404, stops the stream;
 * - `stream.close` is the API's planned end (the token is about to expire,
 *   or the hour is up): the client reconnects after the stream's `retry`
 *   (1 s), with a fresh token when the old one is within its expiry margin;
 * - `stream.gap` means the events after the cursor are gone: the client
 *   forgets the cursor and tells its handler to drop what it holds, and the
 *   new snapshot that the API sends right after replaces it;
 * - a watchdog aborts a connection that receives nothing for 45 s: the API
 *   sends a comment every 15 s, so a silent connection is a dead one that
 *   no error reported (a sleeping laptop, a proxy that dropped it);
 * - the status turns `interrupted` once the stream has been down for 5 s,
 *   for the page's banner, and `open` again on the next connection.
 */
import type { LiveSessionEvent, LiveSessionSnapshot, StreamClose } from "@livecart/api-client";
import { createParser, type EventSourceMessage } from "eventsource-parser";

/** The six events of the session; `stream.gap` and `stream.close` are the client's own business. */
export type SessionEvent = Exclude<LiveSessionEvent, { type: "stream.gap" | "stream.close" }>;

export type StreamStatus =
  /** Connecting, for the first time or again, for less than the outage delay. */
  | "connecting"
  | "open"
  /** Down for longer than the outage delay, and still trying. */
  | "interrupted"
  /** The refresh was refused: the seller must sign in again. */
  | "signed_out"
  /** The session is not the seller's (404). */
  | "not_found"
  | "closed";

export interface StreamHandlers {
  onSnapshot(snapshot: LiveSessionSnapshot): void;
  onEvent(event: SessionEvent): void;
  /** Events after the cursor are gone: drop the state; a snapshot follows. */
  onGap(): void;
  onStatus(status: StreamStatus): void;
}

/** The part of the auth session the stream needs (`AuthSession` is one). */
export interface StreamAuth {
  accessToken(): string | null;
  /** A new token, `null` when refused; rejects when the server cannot be asked. */
  refresh(): Promise<string | null>;
}

export interface StreamOptions {
  url: string;
  send: (request: Request) => Promise<Response>;
  auth: StreamAuth;
  handlers: StreamHandlers;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  watchdogMs?: number;
  outageMs?: number;
  /** Jitter source, in [0, 1); injected by tests. */
  random?: () => number;
}

export interface LiveStream {
  close(): void;
}

export const DEFAULTS = {
  initialBackoffMs: 1_000,
  maxBackoffMs: 30_000,
  watchdogMs: 45_000,
  outageMs: 5_000,
};
/** What a planned close waits when the stream named no `retry`. */
const PLANNED_RECONNECT_MS = 1_000;
/** A snapshot is a few hundred kB at most; a stream buffering more is broken. */
const MAX_BUFFER_CHARS = 8 * 1024 * 1024;
const SESSION_EVENTS: ReadonlySet<string> = new Set<SessionEvent["type"]>([
  "intent.recorded",
  "comment.rejected",
  "order.updated",
  "stock.updated",
  "shipment.updated",
  "session.updated",
]);

type Next = { kind: "retry"; delayMs: number } | { kind: "stop"; status: StreamStatus };
type StreamEnd = { kind: "ended" } | { kind: "closed"; reason: StreamClose["data"]["reason"] };

class Watchdog {
  #timer: ReturnType<typeof setTimeout> | undefined;
  readonly #ms: number;
  readonly #onSilence: () => void;

  constructor(ms: number, onSilence: () => void) {
    this.#ms = ms;
    this.#onSilence = onSilence;
  }

  feed(): void {
    clearTimeout(this.#timer);
    this.#timer = setTimeout(this.#onSilence, this.#ms);
  }

  stop(): void {
    clearTimeout(this.#timer);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Let go of an answer's body without waiting: only its status mattered. */
function discard(response: Response): void {
  response.body?.cancel().catch(() => undefined);
}

function retryAfterMs(response: Response): number {
  const seconds = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
}

export function openLiveStream(options: StreamOptions): LiveStream {
  const { url, send, auth, handlers } = options;
  const initialBackoffMs = options.initialBackoffMs ?? DEFAULTS.initialBackoffMs;
  const maxBackoffMs = options.maxBackoffMs ?? DEFAULTS.maxBackoffMs;
  const watchdogMs = options.watchdogMs ?? DEFAULTS.watchdogMs;
  const outageMs = options.outageMs ?? DEFAULTS.outageMs;
  const random = options.random ?? Math.random;

  let closed = false;
  let status: StreamStatus | null = null;
  let lastEventId: string | null = null;
  let failures = 0;
  let unauthorizedInARow = 0;
  let serverRetryMs: number | null = null;
  let connection: AbortController | null = null;
  let outageTimer: ReturnType<typeof setTimeout> | undefined;
  let wake: (() => void) | null = null;

  // Read through a call: close() changes it while a connection is awaited.
  const isClosed = (): boolean => closed;

  function setStatus(next: StreamStatus): void {
    if (next !== status) {
      status = next;
      handlers.onStatus(next);
    }
  }

  /** Down from now: the banner shows once this lasts `outageMs`. */
  function outageStarts(): void {
    outageTimer ??= setTimeout(() => {
      setStatus("interrupted");
    }, outageMs);
    if (status !== "interrupted") {
      setStatus("connecting");
    }
  }

  function outageEnds(): void {
    clearTimeout(outageTimer);
    outageTimer = undefined;
  }

  function backoff(atLeastMs = 0): Next {
    failures += 1;
    const exponential = Math.min(maxBackoffMs, initialBackoffMs * 2 ** (failures - 1));
    const jittered = Math.min(maxBackoffMs, exponential + random() * initialBackoffMs);
    // The cap bounds the client's own backoff, never the server's Retry-After.
    return { kind: "retry", delayMs: Math.max(jittered, atLeastMs) };
  }

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        wake = null;
        resolve();
      }, ms);
      wake = () => {
        clearTimeout(timer);
        wake = null;
        resolve();
      };
    });
  }

  /** A token to connect with: the current one, a refreshed one, or why there is none. */
  async function tokenToSend(): Promise<string | Next> {
    const current = auth.accessToken();
    if (current !== null) {
      return current;
    }
    try {
      const refreshed = await auth.refresh();
      return refreshed ?? { kind: "stop", status: "signed_out" };
    } catch {
      return backoff();
    }
  }

  /** Hand one message to its handler; a `stream.close` ends the connection. */
  function dispatch(message: EventSourceMessage): StreamEnd | null {
    let data: unknown;
    try {
      data = JSON.parse(message.data);
    } catch {
      return null;
    }
    if (!isRecord(data)) {
      return null;
    }
    const name = message.event ?? "message";
    if (name === "snapshot" && typeof data.events_cursor === "string") {
      // Checked by the API against its schema before it is sent.
      handlers.onSnapshot(data as unknown as LiveSessionSnapshot);
    } else if (name === "stream.gap") {
      // The cursor names events that are gone: never resume from it.
      lastEventId = null;
      handlers.onGap();
      return null;
    } else if (name === "stream.close" && isRecord(data.data)) {
      return { kind: "closed", reason: (data as unknown as StreamClose).data.reason };
    } else if (SESSION_EVENTS.has(name) && data.type === name) {
      handlers.onEvent(data as unknown as SessionEvent);
    } else {
      return null;
    }
    if (message.id !== undefined && message.id !== "") {
      lastEventId = message.id;
    }
    return null;
  }

  async function read(body: ReadableStream<Uint8Array>, watchdog: Watchdog): Promise<StreamEnd> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    // Written by the parser's callbacks, read after each chunk.
    const state: { end: StreamEnd } = { end: { kind: "ended" } };
    const parser = createParser({
      maxBufferSize: MAX_BUFFER_CHARS,
      onEvent: (message) => {
        state.end = dispatch(message) ?? state.end;
      },
      onRetry: (ms) => {
        serverRetryMs = ms;
      },
    });
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) {
        return state.end;
      }
      watchdog.feed();
      parser.feed(decoder.decode(chunk.value, { stream: true }));
      if (state.end.kind === "closed") {
        reader.cancel().catch(() => undefined);
        return state.end;
      }
    }
  }

  async function connectOnce(): Promise<Next> {
    const token = await tokenToSend();
    if (typeof token !== "string") {
      return token;
    }
    const controller = new AbortController();
    connection = controller;
    const watchdog = new Watchdog(watchdogMs, () => {
      controller.abort(new Error(`No message for ${String(watchdogMs)} ms`));
    });
    watchdog.feed();
    try {
      const headers = new Headers({
        Accept: "text/event-stream",
        Authorization: `Bearer ${token}`,
      });
      if (lastEventId !== null) {
        headers.set("Last-Event-ID", lastEventId);
      }
      const response = await send(
        new Request(url, { headers, cache: "no-store", signal: controller.signal }),
      );
      if (response.status === 401) {
        discard(response);
        unauthorizedInARow += 1;
        try {
          if ((await auth.refresh()) === null) {
            return { kind: "stop", status: "signed_out" };
          }
        } catch {
          return backoff();
        }
        // A fresh token goes at once; one the API refuses again waits its turn.
        return unauthorizedInARow > 1 ? backoff() : { kind: "retry", delayMs: 0 };
      }
      if (response.status === 404) {
        discard(response);
        return { kind: "stop", status: "not_found" };
      }
      if (!response.ok || response.body === null) {
        discard(response);
        return backoff(retryAfterMs(response));
      }

      failures = 0;
      unauthorizedInARow = 0;
      outageEnds();
      setStatus("open");
      const end = await read(response.body, watchdog);
      if (end.kind === "closed" && end.reason !== "unavailable") {
        return { kind: "retry", delayMs: serverRetryMs ?? PLANNED_RECONNECT_MS };
      }
      // The API's own failure (unavailable), or a stream that ended by itself.
      return backoff();
    } catch {
      // A network error, or the watchdog's abort.
      return isClosed() ? { kind: "stop", status: "closed" } : backoff();
    } finally {
      watchdog.stop();
      connection = null;
    }
  }

  async function run(): Promise<void> {
    outageStarts();
    while (!isClosed()) {
      const next = await connectOnce();
      if (isClosed()) {
        return;
      }
      if (next.kind === "stop") {
        outageEnds();
        setStatus(next.status);
        return;
      }
      outageStarts();
      if (next.delayMs > 0) {
        await sleep(next.delayMs);
      }
    }
  }

  void run();

  return {
    close() {
      if (closed) {
        return;
      }
      closed = true;
      outageEnds();
      connection?.abort();
      wake?.();
      setStatus("closed");
    },
  };
}
