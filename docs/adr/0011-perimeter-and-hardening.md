# ADR 0011: Perimeter and hardening: same origin through nginx, one trusted proxy, rate limits, least-privilege roles, hardened containers

## Status

Accepted, 2026-09-26. Milestone 1 put nginx, the PostgreSQL roles and the hardened containers in place; milestone 3 added the security headers of the API, the trusted hosts, the body limit and the rate limits. The limit per platform of the comment webhook arrives with the webhook, in milestone 4a.

## Context

LiveCart is a public portfolio project that must hold up as if it were in production. The requirements ask for restrictive CORS, security headers, rate limiting per IP and per seller, no secret in the repository, a least-privileged PostgreSQL user and non-root containers. A rate limit per IP only means something if a client cannot choose its IP, and a phone on the LAN must reach the API during development without opening the API itself to the network.

## Decision

### One origin, one entry point

- nginx (the web container, `nginxinc/nginx-unprivileged`) serves the dashboard and forwards `/api/` and `/health` to the API, so the browser and the mobile app talk to one origin: there is no CORS to configure, and the API has no CORS middleware, so no answer lets another origin read it. nginx listens on 8080, on every interface; the API is published on `127.0.0.1:8000` only, PostgreSQL and Redis on `127.0.0.1` with non-standard ports.
- nginx caps request bodies at 1 MiB (`client_max_body_size`), and so does the API (Starlette's `RequestBodyLimitMiddleware`, 413), for anything that reaches it otherwise.
- The configuration of nginx is a static file, `infra/nginx/default.conf`, without templating. It sets the dashboard's security headers on the SPA location only (`Content-Security-Policy: default-src 'self'; connect-src 'self'; frame-ancestors 'none'`, `nosniff`, `no-referrer`), so that the answers it forwards carry the API's own.

### nginx is the only trusted hop

- nginx overwrites `X-Forwarded-For` with its peer's address (`$remote_addr`), never appending to it, and sets `X-Forwarded-Proto`. uvicorn runs with `--proxy-headers --forwarded-allow-ips ${NGINX_IP}`: it rewrites the client address from those headers only when the connection comes from nginx, never `*`.
- nginx has a fixed address (`NGINX_IP`, 172.30.0.10) in a fixed compose subnet, and the other containers get their dynamic addresses from its upper half only (`ip_range`), so no container can take nginx's address.
- The tests serve the app through uvicorn's own stack, built from the api command of `compose.yaml` as uvicorn parses it: two `X-Forwarded-For` values sent by nginx get counters of their own, one forged by any other peer is ignored, and two IPv6 addresses of one /64 share a counter.

### Headers and hosts of the API

- Every answer, the refusals of the perimeter included, carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`: a JSON answer is never sniffed, framed or rendered. `HSTS_ENABLED` adds `Strict-Transport-Security: max-age=31536000; includeSubDomains`, once the API is only reached over HTTPS. The Swagger UI page of development keeps every header but the CSP; production serves neither `/docs` nor `/openapi.json`.
- `TrustedHostMiddleware` answers 400 to a `Host` outside `ALLOWED_HOSTS` (`localhost,127.0.0.1,api` by default; 127.0.0.1 for the container healthcheck, api for the other containers), without redirecting. The settings refuse `*` and anything that is no host name.

### Rate limits in Redis

- Fixed windows counted in Redis by one Lua script, loaded with `register_script`: `INCR`, then `PEXPIRE ... NX`, so that only the first hit of a window sets its end, under `{p}:rl:{rule}:{subject}`. Every API process counts in the same windows. Beyond a limit, the answer is 429 with `Retry-After`.
- The limits (configurable in the settings, validated at startup):

  | Route                                    | Limit                                 | Counted per                                               |
  | ---------------------------------------- | ------------------------------------- | --------------------------------------------------------- |
  | `POST /api/v1/auth/login`                | 10 a minute                           | client address                                            |
  | failed logins                            | 5 per 15 minutes                      | (account, client address)                                 |
  | every seller route                       | 300 a minute                          | seller                                                    |
  | `GET /api/v1/orders/{reference}`         | 30 a minute, and 20 not-found answers | client address                                            |
  | `GET /api/v1/orders/{reference}/details` | 60 a minute                           | SHA-256 of the order token, or client address without one |

- IPv6 addresses count by /64, and IPv4-mapped ones as IPv4. The account of a failed login counts by the SHA-256 of its lower-case e-mail, and a token by its SHA-256, so neither reaches Redis. Not-found lookups are also counted for all addresses together, and a warning is logged once per window past `BUYER_LOOKUP_NOT_FOUND_ALERT_PER_MINUTE` (100).
- When Redis cannot be read, logins answer 503, since their limits are what stops password guessing; the refresh has no limit; every other route lets requests through, and the failure is logged and counted (`livecart_rate_limiter_errors_total`, beside `livecart_buyer_lookup_not_found_total`, plain counters until the metrics port exposes them).

### Secrets

- `.env` is never committed; `.env.example` holds `change-me` values only, and every process refuses to start in production while any of its `SecretStr` settings holds one. `compose.yaml` requires each secret with `${VAR:?}`, without a default. `JWT_SECRET` and `ORDER_TOKEN_KEY` are at least 32 bytes and must differ.

### Least-privilege PostgreSQL roles

- `livecart_migrator` owns the databases (`livecart`, `livecart_test`) and the schema, and runs the migrations, the demo seed and the test fixtures. `livecart_app`, the role of the API and the worker, holds `SELECT`, `INSERT` and `UPDATE` on the tables, never `DELETE` or `TRUNCATE`, and nothing on `alembic_version`. Both are `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, and `PUBLIC` may connect to no database, the maintenance ones included.
- The API and worker containers receive the `livecart_app` URL only; `migrate` and `seed` receive the migrator's.

### Hardened containers

- One Python image for the API, the worker, `migrate` and `seed`: a non-root user (uid 10001), its virtual environment owned by root, bytecode compiled at build time (`UV_COMPILE_BYTECODE=1`). In compose they run `read_only` with a 16 MiB `/tmp` (`noexec`, `nosuid`, `nodev`), `cap_drop: [ALL]`, `no-new-privileges` and a limit of 1 CPU and 512 MiB.
- The web container runs nginx-unprivileged (uid 101), `read_only` with its cache and `/tmp` on tmpfs, `cap_drop: [ALL]` and `no-new-privileges`.
- PostgreSQL and Redis run their official images, whose entrypoints drop to an unprivileged user, with resource limits but without `read_only` or `cap_drop`, which would break their first `chown`. Redis requires a password and runs with `maxmemory 256mb`, `noeviction` and an append-only file.

## Consequences

- The limits per address are only as good as the one trusted hop: a proxy placed in front of nginx has to be trusted by nginx in turn, never by opening uvicorn to every peer.
- A fixed window lets through up to twice a limit around its edge; a sliding window is not implemented.
- A Redis outage closes logins, while signed-in sellers and buyers carry on.
- A phone on the LAN goes through nginx on port 8080, open on the LAN in development, and needs the PC's LAN address in `ALLOWED_HOSTS`.
- After the API container is recreated with a new address, nginx keeps sending to the old one until its resolver re-reads it, within 10 s: requests through nginx fail meanwhile.
- No process of the stack can delete a row but the migrator's: a purge, when there is one, runs as the migrator.

## Alternatives considered

- **A CORS allow-list**: needed only if the clients were served from other origins; one origin needs none, and allows `SameSite=Strict` cookies.
- **`FORWARDED_ALLOW_IPS=*`**: any client could name the address its limits count.
- **Rate limits in nginx (`limit_req`)**: counted per nginx instance, and blind to sellers, tokens, accounts and not-found answers, which the application knows.
- **A rate-limiting library such as slowapi**: per-process memory by default, and no say in what fails open or closed when the store is down.
- **One database role for everything**: a compromised API could alter the schema or delete the history.
- **Containers running as root with a writable filesystem**: a compromised process could change its own code.
