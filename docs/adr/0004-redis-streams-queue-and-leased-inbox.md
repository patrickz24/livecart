# ADR 0004: Comment queue: Redis Streams with backlog backpressure and MINID trimming, a leased PostgreSQL inbox

## Status

Accepted, 2026-09-26. Milestone 4a implemented both sides: the signed webhook that queues the comments, with its backpressure, and the worker that consumes them, with the inbox's lease, the reclaim, the dead letters and the trim.

## Context

During a live, comments arrive in bursts, and a platform expects its webhook answered at once. Each comment may then reserve stock, or call an LLM: work that must happen once, whereas every link of the chain may deliver twice (a platform retrying a webhook, a queue handing a comment out again after a crash). A worker may die anywhere, between two statements or between a commit and its acknowledgment, and a deployment runs several workers. Redis has a bounded memory, which the queue shares with the rate limits and, from milestone 4b, the session streams. The mechanism has to be explained command by command in an interview.

## Decision

### One stream, behind the `CommentQueue` port

- `RedisCommentQueue`: the stream `{prefix}:queue:comments`, read by the consumer group `workers`, and its dead-letter stream `{prefix}:queue:comments:dlq`. Each entry holds one envelope (`QueuedComment`: the comment, its `correlation_id`, the `request_id` of the webhook call, `enqueued_at`).
- The webhook verifies the signature, then queues a batch with one `XADD` per comment in one pipeline, without `MAXLEN`, which would drop comments nobody has read. A pipeline is not a transaction: a partial failure answers 503, and the platform's retry is deduplicated by the inbox. Nothing on this path touches PostgreSQL.
- Redis runs with `appendonly yes`, `appendfsync everysec`, a `maxmemory` of 256 MiB and `noeviction`: a crash of Redis loses about a second of writes at most, comments the webhook already answered included, and a full Redis refuses writes rather than evicting queue entries. At startup, `QUEUE_MAX_DEPTH × QUEUE_MAX_ENVELOPE_BYTES × 5/4` must fit in half of `maxmemory`; the 5/4 covers the stream's own cost and the allocator's rounding, measured on Redis 8.10.2.

### Backpressure on the backlog, not on the length

- The backlog is what the workers have not acknowledged yet: `lag + pending` of the group, from `XINFO GROUPS` (Redis 7.0), and `XLEN`, an upper bound, when Redis cannot tell the lag. It is read at most once a second per API instance, plus what that instance queued since. At `QUEUE_MAX_DEPTH` (20 000), the webhook answers 503 with `Retry-After`.
- The stream's length also counts a settled history that the trim has not dropped yet: comparing it would refuse comments while the workers keep up.

### The inbox and its lease

- `comments` holds one row per (platform, external comment id), `uq_comments_platform_external_comment_id`: the deduplication lives where the effects happen, in the transactions that write them, not in a Redis key that could expire or be lost.
- Unit 1 (`SqlCommentInbox.claim`) records the comment and takes its lease in one statement: `INSERT ... ON CONFLICT (platform, external_comment_id) DO UPDATE SET claimed_by, claimed_until WHERE comments.status = 'received' AND (claimed_until IS NULL OR claimed_until < :now OR claimed_by = :holder) RETURNING`. The lease lasts `MESSAGE_PROCESS_TIMEOUT_S` (30 s), the deadline of the handler, so it outlives the handler that the worker cancels at that deadline.
- The lease belongs to one delivery, `claimed_by = <consumer>/<delivery id>`, rather than to a consumer: an in-process retry of a delivery, after a transient error or a commit whose acknowledgment was lost, takes its own lease again, while a second delivery handled by the same worker at the same time does not.
- A delivery that does not get the lease finds the comment either settled, a duplicate whose stored outcome is published again and whose buyer is notified if no notice went out (ADR 0007) before its `XACK`, or in flight under another delivery, and then only acknowledges: the holder's own entry stays pending, and is reclaimed if its worker dies.
- Every write that settles a comment (unit 3, a rejection, a dead letter) carries the guard `status = 'received'`, so a comment settles once, whatever the number of deliveries racing for it (`ProcessComment`).

### The consumer

- Each worker runs one `CommentConsumer`: `XREADGROUP GROUP workers <host>-<pid> COUNT <room left> BLOCK 2000`, within a window of `WORKER_BATCH` (32) deliveries held at once, by at most `WORKER_CONCURRENCY` (16) handlers (`asyncio.Semaphore`), each within `asyncio.timeout_at` its start plus `MESSAGE_PROCESS_TIMEOUT_S`, a deadline handed to the handler, so that the LLM fallback gets what is left of it (ADR 0006).
- `ServiceUnavailable`, a transient failure of PostgreSQL (lock or statement timeout, lost connection) or of Redis, is retried twice in the process, after delays drawn from 0 to 0.1 s, then 0 to 0.2 s (full jitter). After that, or at the deadline, the delivery is left pending.
- The pool holds a connection per handler and one per database loop (4), plus 2 on overflow: 22, which `WORKER_DB_MAX_CONNECTIONS` caps at startup, so that one API and three workers stay within PostgreSQL's 97 connections.

### Reclaim, dead letters and trim

- Every 30 s, at startup first, the consumer's loop reclaims (`XAUTOCLAIM`, `ReclaimComments`) the deliveries idle for `QUEUE_RECLAIM_MIN_IDLE_MS`, as many as its window has room for, between two reads: the loop is the only one to fill the window, so the reclaimed deliveries count in it like the others.
- The idle time derives from the window: a live worker holds a delivery at most `ceil(WORKER_BATCH / WORKER_CONCURRENCY)` deadlines (it waits for the handlers ahead of it, then runs its own), 60 s by default, and the idle time is twice that, 120 s. A value set below it is refused at startup: it would reclaim deliveries a live worker holds, and count deliveries that never failed towards the dead-letter queue.
- A delivery counted more than `QUEUE_MAX_DELIVERIES` (3) times goes to the dead-letter queue: the inbox first (`dead_lettered`, under the guard; a comment that no delivery ever recorded is recorded so), then the queue, where one Lua script acknowledges the entry and, only if it was still pending, copies it to the dead-letter stream (`MAXLEN ~ 1000`: the inbox row is the durable record). In that order, a crash between the two repeats both at the next tick, the inbox's part as a no-op. A comment settled otherwise keeps its outcome, and its entry still leaves the stream.
- Each tick then trims the stream with `XTRIM MINID ~` below the oldest pending entry and the group's last-delivered id, whichever is lower. Every entry below both was delivered and settled, and none can become unread or pending again, so the trim never drops a comment that is new or pending.

### What proves it

- `tests/contract/test_comment_queue.py` and `test_comment_inbox.py`: the queue and the inbox on their fakes and on Redis or PostgreSQL alike (ADR 0002).
- `tests/integration/test_redis_comment_queue.py` and the worker's `test_reclaim_redis.py`: the pending entries list, a reclaim after a real idle time, the dead-letter stream and `dead_lettered`, the trim that keeps every pending and unread entry.
- `tests/integration/test_process_comment_concurrency.py`: twenty parallel deliveries of one comment give one intent and one order, and a thousand concurrent comments reserve exactly the 200 units in stock, through the worker's pool.
- The worker's `test_consumer_redis.py`: every delivery acknowledged, a real lock timeout retried in the process, and what a crashed consumer left taken back by `serve` at the shortest idle time the settings allow.

## Consequences

- An effect happens once per comment, and at most one delivery works on a comment at a time, which, with the receipt stored once in the inbox, bounds the LLM calls to one per comment (ADR 0006).
- The order is first come, first served at best: a reclaimed comment comes back after newer ones.
- After a worker dies, its deliveries wait for the idle time before another worker takes them: about two minutes with the defaults, about one with a window of 16.
- Every comment leaves a row in the inbox, and nothing purges them yet (a purge job is on the roadmap).
- Consumers named `<host>-<pid>` stay listed in the group after their process ends, with nothing pending once reclaimed; a container restarted in place keeps its name.
- While Redis is down, the webhook answers 503 and the worker's loops log and retry; its heartbeat stops, so the container reports unhealthy.

## Alternatives considered

- **arq, Celery, RabbitMQ or Kafka**: a job framework or another broker to run and explain, for what a stream, a consumer group, `XAUTOCLAIM` and a dead-letter stream give command by command.
- **Deduplication with Redis `SET NX`**: a second store of truth beside PostgreSQL, whose key may expire or be lost while the effects stay; the inbox is written in the same database as the reservation.
- **Backpressure on `XLEN`**: it counts the settled history too, so it would refuse comments while the workers keep up.
- **`XADD ... MAXLEN`**: it drops the oldest entries, read or not, which are exactly the comments backpressure exists to keep.
- **A lease per consumer rather than per delivery**: two deliveries of one comment handled by the same worker at once would both take it.
- **One transaction around the LLM call**: a connection held for seconds per ambiguous comment; the three short units keep the effect unique through the lease and the guard instead.
- **A reclaim loop of its own, beside the read loop**: built first, it found no room in the window whenever it looked, since the read loop spends its time in `XREADGROUP BLOCK` with the room it asked for set aside, so an idle worker never took back what a crashed one had left (found by killing a worker of the compose stack). Reclaiming between two reads delays the next read by the tick only: a few milliseconds, or a dead letter's transaction.
