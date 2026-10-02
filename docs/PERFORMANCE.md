# Performance

> **Three measured runs of one scenario on one laptop.** On 2026-10-02, three back-to-back runs of the k6 comment spike each posted 1,000 signed comment webhooks on Z99 of the `loadtest` session (200 in stock). Every call was acknowledged with 202, the 200 units were reserved by orders holding exactly 200 units, and the `no_oversell` check passed each time. Every result below is quoted from the three JSON summaries in [`loadtest/results/`](../loadtest/results/). The load generator and the stack shared the machine, so these figures describe this laptop on that evening. They are not a capacity claim.

## Machine

| Item                     | Value                                                                   |
| ------------------------ | ----------------------------------------------------------------------- |
| CPU                      | 12th Gen Intel Core i7-1255U, 10 cores, 12 logical processors           |
| RAM                      | 15.7 GiB (16,839,684,096 bytes)                                         |
| OS                       | Windows 11 Pro 10.0.26200                                               |
| Docker                   | Docker Desktop 4.92.0 (240144), engine 29.8.0, Linux containers         |
| WSL                      | WSL 2.2.4.0, kernel 5.15.153.1-microsoft-standard-WSL2, no `.wslconfig` |
| Seen by the engine       | `docker info`: 12 CPUs, total memory 8,161,259,520 bytes (7.6 GiB)      |
| Other running containers | Patrick's, untouched during the runs (below)                            |

Running beside the stack during the counted runs, as background load (`docker ps` at the start of each run):

- `supabase_db_sequences-local` (`supabase/postgres:17.11.0.002`), `supabase_kong_sequences-local` (`supabase/kong:2.8.1`) and `supabase_rest_sequences-local` (`supabase/postgrest:v16.4`), idle;
- `engage-mariadb` (`mariadb:10.2.44`).

An earlier series of three runs that day ran under different background load, with a container (`engage-back`) restarting in a loop, and is not counted.

## Stack configuration as it ran

Read from the containers' environment, from PostgreSQL's `pg_settings`, from Redis's `CONFIG GET` and from `livecart_core.infrastructure.db.engine`, after the runs, in the containers that served them:

- **Services:** one API and one worker. The API is a single uvicorn process. The simulator was stopped for the series (`docker compose stop simulator`), and the dashboard's nginx was up but outside the path: k6 posted straight to `api:8000`, as the simulator does. Compose caps each of the API, the worker, PostgreSQL and Redis at 1 CPU and 512 MiB, and k6 at 1 CPU and 1 GiB.
- **API:** a pool of 5 PostgreSQL connections plus 10 on overflow, which the webhook path does not use. `QUEUE_MAX_DEPTH=20000` and `QUEUE_MAX_ENVELOPE_BYTES=5120` set the backpressure; `RATE_LIMIT_COMMENT_WEBHOOK_PER_MINUTE=6000` is the platform's rate limit, and `ENABLED_PLATFORMS=simulator`.
- **Worker:** `WORKER_CONCURRENCY=16` comment handlers and `WORKER_BATCH=32`. Its pool holds 20 connections (16 handlers and 4 background loops) plus 2 on overflow, within `WORKER_DB_MAX_CONNECTIONS=22`. Also `MESSAGE_PROCESS_TIMEOUT_S=30` and `QUEUE_MAX_DELIVERIES=3`. No `ANTHROPIC_API_KEY`: the parser alone reads the comments, and every comment of the scenario is one it reads without the LLM fallback.
- **PostgreSQL 16.15:** `max_connections` 100, `shared_buffers` 128 MiB (16384 × 8 kB), `synchronous_commit` on. Each connection sets `statement_timeout` 5 s and `lock_timeout` 2 s.
- **Redis 8.10.2:** `maxmemory` 268,435,456 bytes (256 MiB) with `noeviction`, append-only file flushed every second.
- **Both:** `APP_ENV=development`, `LOG_LEVEL=INFO`, one JSON log line per event on stdout.

## The scenario, and why

[`loadtest/comment-spike.ts`](../loadtest/comment-spike.ts), run by `grafana/k6:2.3.0` in the compose profile `loadtest` (CONTRIBUTING.md, "Load test"; [ADR 0015](adr/0015-quality-tooling-and-supply-chain.md)):

- **An open model** (`ramping-arrival-rate`): calls arrive on schedule whatever the API's latency, as a live audience does. The rate climbs to 80 calls a second in 5 s, holds, then falls back in 5 s, so the burst lasts about 17.5 s. 80 a second stays below the webhook's limit of 6,000 a minute, so the runs load the queue and the workers, not the rate limiter.
- **1,000 calls, one comment each, each from its own buyer,** as one live audience asking for the same product. They ask for one, two or three units, in the simulator's proportions (70 %, 20 %, 10 %). Every call is signed with the simulator platform's secret, as a platform signs its webhooks.
- **Replays:** one call in ten sends again the comment of five calls before it, as a platform's retry would. That makes 900 distinct comments and 100 replays, which the inbox must count once. The 900 buyers want about 1,260 units of 200, so the last units are fought over.
- **A reset before each run** (`docker compose run --rm seed --reset-loadtest`): the scenario sends the same comment ids at every run, and the session must start as seeded, which its setup checks.

**What `no_oversell` checks, and how.** The webhook answers 202 once a comment is queued, and a worker reserves the stock later. So `teardown()` first waits for the queue to drain: it reads the worker's `livecart_queue_backlog` (lag plus pending of the consumer group) until it reads 0. The worker samples that backlog at each reclaim, every 30 s, so the first reading comes 40 s after the burst, the earliest one sure to postdate it. Then it reads the session's stock and orders in one consistent read: the snapshot that opens the session's event stream, the one the seller's dashboard reads, and the only API read that lists a session's orders.

k6 has no streaming client, so a 3 s timeout ends that request, and k6 keeps the bytes that arrived before it. Every run logs this as one failed request. The check fails closed: a snapshot that cannot be read or parsed fails `no_oversell`, and so does one whose order list is full (200, the snapshot's limit), since a full list may hide orders. The mix of quantities keeps the number of orders under that limit. It passes only when:

- Z99's stock is still 200;
- the orders that hold stock (pending payment or paid) hold at most 200 units;
- those orders hold exactly the units the product's counters say are reserved and sold.

**What fails a run:** `no_oversell`; `queue_drained` (no backlog of 0 within 180 s of the first reading); fewer than 1,000 calls sent; any 429, any 503 or any other answer than 202. Each of those has its own counter. There is no latency threshold.

## Results

The three runs ran back to back, on the stack brought up with `docker compose up -d --wait`, with the simulator stopped. The summaries are named after the time each run finished:

| Run | Summary                                                                                        | Finished (UTC)           |
| --- | ---------------------------------------------------------------------------------------------- | ------------------------ |
| 1   | [comment-spike-20261002T164442Z.json](../loadtest/results/comment-spike-20261002T164442Z.json) | 2026-10-02T16:44:42.514Z |
| 2   | [comment-spike-20261002T164559Z.json](../loadtest/results/comment-spike-20261002T164559Z.json) | 2026-10-02T16:45:59.610Z |
| 3   | [comment-spike-20261002T164716Z.json](../loadtest/results/comment-spike-20261002T164716Z.json) | 2026-10-02T16:47:16.600Z |

### Correctness

| Run    | Calls | 202  | 429 | 503 | Other | Orders | Units reserved | Stock | `no_oversell` | `queue_drained` | Backlog first read 0 after |
| ------ | ----- | ---- | --- | --- | ----- | ------ | -------------- | ----- | ------------- | --------------- | -------------------------- |
| 1      | 1000  | 1000 | 0   | 0   | 0     | 144    | 200            | 200   | passed        | passed          | 40.006 s                   |
| 2      | 1000  | 1000 | 0   | 0   | 0     | 144    | 200            | 200   | passed        | passed          | 40 s                       |
| 3      | 1000  | 1000 | 0   | 0   | 0     | 144    | 200            | 200   | passed        | passed          | 40.002 s                   |
| Median | 1000  | 1000 | 0   | 0   | 0     | 144    | 200            | 200   |               |                 | 40.002 s                   |

Units reserved is the counter `reserved`. `sold` was 0 in every run, since no buyer paid. In every run, the orders held exactly those 200 units, and every threshold passed.

- **The units are the invariant, not the orders.** The three runs ended with 144 orders each, but nothing in the scenario fixes that count: it depends on which buyers, asking for one to three units, win the last units, in the order the worker's 16 handlers settle the comments.
- **Drain time.** The backlog read 0 at the very first reading, which the scenario takes 40 s after the burst. All this says is that the queue was empty within 40 s of the last call; it cannot say how much sooner. The resolution is the worker's sampling interval, about 30 s.
- **The open model held.** Every planned call was sent: `webhook_calls` 1000, and its threshold passed. k6 never needed more than its 80 pre-allocated VUs (`vus_max` 80).

### Acknowledgement latency

`webhook_duration`, the time from k6 sending a webhook call to receiving its answer, in milliseconds, rounded to 0.01 ms (the summaries hold the full values). Each median is the median of the three runs' values:

| Run    | p50  | p95  | p99  | max    |
| ------ | ---- | ---- | ---- | ------ |
| 1      | 2.76 | 4.01 | 5.95 | 9.74   |
| 2      | 2.76 | 3.90 | 8.37 | 411.36 |
| 3      | 2.79 | 4.31 | 5.29 | 18.60  |
| Median | 2.76 | 4.01 | 5.95 | 18.60  |

Run 2 had one call far slower than all the others: its max is 411.36 ms against a p99 of 8.37 ms. A single call in 1,000 barely moves the percentiles, and three runs cannot say what caused it.

## What is measured, and what is not

- **Measured: the webhook's acknowledgement.** That covers checking the signature, counting the call against the platform's rate limit, and queueing the comment in Redis: the API's whole part, which never touches PostgreSQL.
- **Not measured: the end-to-end processing time.** That would be the time from a comment's call to its reservation or rejection. The workers do that work later, and the worker's own metrics (`livecart_queue_wait_seconds`, `livecart_comment_processing_seconds`) were not captured. The drain time above is an upper bound, about 30 s coarse.
- **Not a throughput.** The `rate` values in the summaries' k6 metrics, such as `webhook_calls.rate`, are averaged over the whole test, including the 40 s drain wait and the reads around it. The calls' rate is the scenario's schedule: up to 80 a second.
- **Not the HTTP totals.** `http_req_duration` and `http_req_failed` in the summaries also count the two sign-ins and the snapshot read, which a timeout ends on purpose. `webhook_duration` is the webhook's own latency.
- **Not through nginx.** k6 posts to the API container directly, so the proxy is not in the measured path.

## Limits

- **The generator and the target share one laptop**, in the same Docker VM, so k6's CPU competes with the API's, the worker's, PostgreSQL's and Redis's. Each service is capped at 1 CPU, of the 12 the engine sees.
- **Background load:** the four containers listed under [Machine](#machine) ran during the series.
- **One API process, one worker, one product.** No scaling was measured. Every reservation waits on the same row lock, which is the case the claim is about, not a typical workload.
- **Three runs, back to back, on one evening, at one rate below the rate limit.** Nothing here looked for the point where the stack stops keeping up: this is evidence that 1,000 concurrent purchase comments do not oversell on this machine, not a capacity figure.
- **Development settings:** `APP_ENV=development` and INFO logs on stdout, collected by Docker Desktop.

## How to reproduce

From the repository root, in Git Bash (CONTRIBUTING.md, "Load test", gives each command and what it does):

```bash
docker compose up -d --wait        # the whole stack, healthy
docker compose stop simulator      # no other comments on the webhook and the worker
make load-test                     # three times, back to back: a reset, then one run each
docker compose start simulator
```

Each run writes `loadtest/results/comment-spike-<UTC time>.json` and exits 99 when a threshold fails. A run that fails is kept and reported, never retried in silence.
