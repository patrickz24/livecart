# ADR 0010: Seller auth: short JWT, rotating refresh token with a 30 s grace, argon2id

## Status

Accepted, 2026-09-26. Milestone 3 implemented the login, the refresh, the logout, `GET /api/v1/auth/me`, the `CurrentSeller` dependency of every seller route and `livecart-create-seller`. The dashboard that uses them arrives in milestone 7.

## Context

Sellers sign in to the dashboard, in a browser, often with several tabs open during a live. The requirements ask for a short-lived JWT, a rotating refresh token and argon2 hashes. A stolen credential must stop working soon, a replayed refresh token must be detected, a script injected in the page must not be able to carry the session away, and two tabs refreshing at the same moment must not log the seller out in the middle of a live. There is no public sign-up.

## Decision

### Access token: a JWT of 15 minutes

- PyJWT 2.15.0, HS256 under `JWT_SECRET`, at least 32 bytes (checked by the settings, and by PyJWT with `enforce_minimum_key_length`). Decoding pins `algorithms=["HS256"]`: the token's own `alg` header never chooses how it is checked, so `none`, another HMAC and the RS256/HS256 confusion are refused. `iss` (`livecart-api`), `aud` (`livecart-seller`), `sub` (the seller's id), `exp` (900 s) and `jti` are required and checked.
- It is answered in the JSON body, with `Cache-Control: no-store`, and sent back as `Authorization: Bearer`. `CurrentSeller`, declared by every seller route, verifies it without reading the database: a missing or refused token is 401 with `WWW-Authenticate: Bearer`.

### Refresh token: rotating, in an HttpOnly cookie

- 256 random bits (`secrets.token_urlsafe(32)`), stored only as their SHA-256 (`uq_refresh_tokens_token_hash`). A login starts a family that lives 30 days: every successor inherits the family's expiry, so a seller signs in again at least once a month.
- Rotation is one transaction: `UPDATE refresh_tokens SET used_at = :now WHERE token_hash = :h AND used_at IS NULL AND revoked_at IS NULL AND expires_at > :now RETURNING ...`, then the successor's insert. Of several refreshes that send one token, the first takes the row lock and the others, once it commits, find `used_at` set and get no row: exactly one rotation wins, without a retry.
- A token already used is either a race between tabs, which share one cookie jar, or a replay. Used less than 30 s ago with its successor still active, it gets a new access token only, **without `Set-Cookie`**: the jar already holds the successor the winning tab received, and no refresh token is ever sent twice. Otherwise the whole family is revoked, the thief's copy and the seller's session with it, and the event is logged as `refresh_token_reuse_detected`. The revocation repeats its `UPDATE` until it revokes nothing more: under READ COMMITTED, one statement waiting on a rotation in flight would miss the successor that rotation inserts, which an integration test shows on PostgreSQL.
- The cookie is `lc_refresh`: `HttpOnly`, `SameSite=Strict`, `Path=/api/v1/auth`, `Secure` in production, with the family's remaining lifetime as its `Max-Age`. `POST /login`, `POST /refresh` and `POST /logout` also require an `Origin` header naming the origin the request was sent to (403 otherwise), a second defence against cross-site requests. The login needs it too because it sets the cookie: a page of another site must never sign a seller's browser into an account of its choosing (login CSRF), and a client outside a browser, such as curl, sends the header itself. A refused refresh answers 401 and clears the cookie; a logout revokes the family and answers 204 whatever the cookie held.

### Passwords: argon2id

- argon2-cffi 25.1.0, argon2id with its default parameters (t=3, 64 MiB, p=4), stored as PHC strings that carry their parameters. Each hash runs in a worker thread, at most four at a time (`asyncio.Semaphore(4)`), so the event loop keeps serving and a burst of logins queues instead of taking 64 MiB each.
- An unknown e-mail, or a locked account, is checked against a dummy hash made with the same parameters when the process starts: it costs what a wrong password costs, and gets the same 401. The e-mail is unique whatever its case (`uq_sellers_email` on `lower(email)`).
- A password is 12 to 128 characters, of any kind, with no composition rule (NIST SP 800-63B). Accounts are created by `livecart-create-seller`, which reads the password from `LIVECART_SELLER_PASSWORD` or asks for it twice without echo, never from its arguments, and by the demo seed.

### Limits

- Logins are limited to 10 a minute per client address, and failed logins to 5 per 15 minutes per (account, address), checked before the password (ADR 0011). While those counters cannot be read (Redis down), logins answer 503. The refresh has no limit and needs no Redis, so signed-in sellers stay signed in through a Redis outage; each seller's requests on the seller routes count against 300 a minute.
- Every time comes from the `Clock` port; PyJWT checks `exp` against the system clock, the same clock in production.

## Consequences

- An access token cannot be revoked before it expires: logging out revokes the refresh family, and the 15 minutes left of the access token are the cost of reading no database per request.
- A script injected in the dashboard can use the access token while the page is open, but can never read the refresh token: hence the strict CSP of ADR 0011.
- Within the 30 s grace, a stolen token whose successor is still active obtains access tokens, not refresh tokens; after it, a replay revokes the family.
- `JWT_SECRET` is one key without rotation: changing it invalidates every access token at once, while the refresh cookies, stored as hashes independent of it, go on issuing new ones.
- Tabs racing on one refresh token keep the seller signed in, and nothing secret is ever sent twice.

## Alternatives considered

- **python-jose**: no longer maintained; PyJWT is.
- **Server-side sessions**: a database read on every request, where a verified JWT needs none.
- **The refresh token in `localStorage`**: readable by any script of the page, where an `HttpOnly` cookie is not.
- **Rotation without a grace**: two tabs refreshing together would revoke the family, and the seller would be logged out during the live.
- **bcrypt or scrypt**: argon2id is the first choice of OWASP's password storage guidance, and the requirement names argon2.
- **RS256**: a key pair helps when third parties verify the tokens; here the API alone issues and checks them.
