# ADR 0017: Dashboard client: in-memory token, one refresh at a time across tabs, a fetch-based event stream

## Status

Accepted, 2026-10-02. Milestone 7 implemented it in `apps/web`: `src/auth/` (the session, its refresh and the 401 retry), `src/realtime/` (the stream client and the reducer) and the pages that use them.

## Context

The API side of the seller's sign-in (ADR 0010) and of the live stream (ADR 0009) leaves the browser several choices that matter for security and for a seller in the middle of a live. The access token lasts 15 minutes and must not be readable by a script that outlives the page; the refresh cookie rotates on every use, and a reused one revokes the whole family past a 30 s grace; several tabs of one seller share that cookie. The stream needs `Authorization: Bearer`, which `EventSource` cannot send, ends itself 5 s before the token expires, can announce a gap, and sends a comment every 15 s. The SPA runs under `default-src 'self'` (ADR 0011): no inline script, no `eval`. The API restarts (`docker compose restart api`) must not log the seller out or lose events.

## Decision

### The access token

- It lives in one object of the tab (`AuthSession`), never in `localStorage`, `sessionStorage` or a cookie a script can read. A reload drops it: `RequireAuth` trades the refresh cookie for a new one before showing a page, and sends the seller to `/login` when the API refuses.
- The generated client's `auth` callback puts it on the routes of the seller scheme only (`SellerAccessToken`), never on a buyer route's order-token scheme. It counts as expired 30 s before its expiry, so a request never leaves with a token that dies on the way.
- A request answered 401 is sent once more with a refreshed token, its body kept for the retry. Requests that fail together refresh once; a request whose token another one already replaced is retried with the new token without refreshing.
- A refresh refused (401, or 403 for the Origin check) signs the tab out and clears the query cache. A refresh that gets no answer, or a 5xx such as nginx's 502 while the API restarts, is no verdict: the session stays as it was and the caller retries.

### One refresh at a time

- In a tab, concurrent callers share the request in flight.
- Across tabs, the request runs under the Web Locks API, `navigator.locks.request("livecart:auth-refresh", ...)`. A second tab only sends the cookie once the first has received its successor in the shared jar, so no two tabs send the same refresh token and the API's 30 s grace stays a safety net, not the normal path.
- Web Locks only exist in secure contexts. On plain HTTP to a LAN address the tab refreshes without the lock and relies on that grace; `localhost` is a secure context.

### The event stream

`openLiveStream` reads `GET /api/v1/live-sessions/{id}/events` with `fetch` and eventsource-parser 4.1.1:

- a bearer on every request, and `Last-Event-ID` once a message with an id has arrived, so the API resumes after it without a snapshot;
- a dropped connection, a network error, a 5xx or a stream that ends by itself: exponential backoff 1, 2, 4... up to 30 s, plus up to 1 s of jitter, and at least a 429's or a 503's `Retry-After`;
- a 401: one refresh through the session above, then a reconnection at once (with backoff if the new token is refused too); a refused refresh or a 404 stops the stream;
- `stream.close`: a reconnection after the stream's `retry` (1 s), with a fresh token when the old one is within its margin; `unavailable` backs off instead;
- `stream.gap`: the cursor is forgotten and the view marked stale until the snapshot the API sends next; a connection lost in between asks for a snapshot again rather than for the gone cursor;
- a 45 s watchdog: three missed heartbeats abort a connection that no error reported dead (a sleeping laptop, a proxy that dropped it);
- a status for the page: `interrupted`, and its banner, once the stream has been down for 5 s; a planned reconnection within a second never shows it.

### The live state

The stream writes one query cache entry per session (TanStack Query, `["live", id]`), through a pure reducer, `applyEvent`. A snapshot replaces the state; each event is an upsert by the id its payload names, so an event seen twice changes nothing and returns the same object. Lists stay newest first by id (UUID v7 text sorts by creation time) and keep the snapshot's `limit`. Statuses only move forward along the API's machines, so an older order, shipment or session event never overwrites a newer one; stock counters have no order, the last event wins and the next snapshot is the truth. Other pages read the same cache: the session's entry follows `session.updated`, and the order page finds an order's shipment there.

### The CSP

zod decides when it builds each object schema whether to compile its parser with `new Function`, after probing that it may. Under the SPA's CSP the probe fails as intended, yet the browser reports a violation for it, so `zodConfig.ts`, imported first, sets `jitless` before any schema module is evaluated.

## Consequences

- A stolen access token works for 15 minutes at most; a script injected in the page can use the token while the page is open, but can read neither the refresh token nor a stored copy of the access token.
- Every reload and every new tab costs one refresh request.
- A logout revokes the cookie's family. The other tabs of the seller keep their access tokens until they expire, then their refresh is refused and they sign out; they do not hear of the logout at once.
- After an API restart the stream comes back on its own, from its last id: on the compose stack, `docker compose restart api` cut a dashboard's stream for about 4 s and it resumed without a snapshot; a 15 s stop showed the banner after 5 s and resumed after the API was back. While the api container was stopped, a reconnection through nginx hung about 18 s before its 502, until nginx resolved the name again; the watchdog bounds such a wait at 45 s.
- The seller has no order route of its own: `/orders/:reference` shows the public projection by reference, which holds no buyer data, and the shipment and payment time from the live view's cache when the dashboard holds them.

## Alternatives considered

- **The access token in `sessionStorage`**: it would survive a reload, and be readable by any script of the page, for as long as the tab lives.
- **Sharing the token between tabs (BroadcastChannel)**: one refresh for all tabs, but the token then travels to every tab of the origin, and a tab's lifetime no longer bounds it; the lock gives one refresh at a time without that.
- **No cross-tab coordination, the server's grace only**: it works, and is the fallback where Web Locks are missing, but every simultaneous refresh then goes through the reuse path that also detects theft.
- **`EventSource`**: no `Authorization` header, so the token would go in the URL, into the access logs, or the stream would need a cookie of its own.
- **The generated client's `sse.get`**: it retries with a backoff, but has no hook to refresh the token on a 401, no watchdog and no status for a banner; the dashboard still builds the stream's URL and types its messages with the generated client.
- **A WebSocket**: bidirectional, which the dashboard does not need (ADR 0009).
