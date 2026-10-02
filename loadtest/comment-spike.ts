// Comment spike: a burst of signed purchase comments on the one product of the
// seeded `loadtest` session (Z99, 200 in stock), then a check that no unit was
// promised twice. Run it on a freshly reset session (CONTRIBUTING.md, "Load
// test"):
//
//   make load-test
//   # docker compose run --rm seed --reset-loadtest
//   # docker compose --profile loadtest run --rm k6
//
// An open model (ramping-arrival-rate): calls arrive on schedule whether or
// not the API has answered the previous ones, as a live audience does. A
// closed model would slow its own arrivals whenever the API slows down, and
// hide the queueing the test is about.
//
// Each call posts one comment of its own buyer, signed as the simulator signs
// them. One call in REPLAY_EVERY sends again the comment of REPLAY_LAG calls
// before it, as a platform's retry would: the inbox must count it once. The
// webhook answers 202 once a comment is queued, and the workers reserve the
// stock later, so teardown() waits for the queue to drain, then reads the
// session's stock and orders and fails the run on any oversell.
import { check, sleep } from "k6";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Gauge, Trend } from "k6/metrics";

import { SIGNATURE_HEADER, signature } from "./lib/sign.ts";

// --- Settings ---------------------------------------------------------------
// Compose passes the two secrets from .env; `docker compose run -e` resizes
// the burst, as the smoke run of CONTRIBUTING.md does.

const API_URL = __ENV.API_URL ?? "http://api:8000";
// The worker's Prometheus endpoint: the compose network only.
const METRICS_URL = __ENV.METRICS_URL ?? "http://worker:9100/metrics";
const RESULTS_DIR = __ENV.RESULTS_DIR ?? "/results";
const WEBHOOK_SECRET = __ENV.COMMENT_WEBHOOK_SECRET_SIMULATOR ?? "";
const SELLER_PASSWORD = __ENV.SEED_DEMO_PASSWORD ?? "";

// 1000 calls by default, 900 comments and 100 replays: 900 buyers who want
// about 1,260 units of 200, so the last units are fought over.
const COMMENTS = positiveInteger("LOADTEST_COMMENTS", 1000);
// 80 calls a second at the peak: below the comment webhook's 6,000 a minute
// (100 a second, ADR 0011), so that the spike loads the queue and the
// workers, not the rate limiter. That limit counts calls per fixed minute,
// so 1,000 calls cannot reach it in any window, whatever the rate.
const PEAK_RATE = positiveInteger("LOADTEST_PEAK_RATE", 80);
// Up to the peak in 5 s and back down in as many: an audience answers the
// seller's call within seconds, and k6 needs a moment to start its VUs.
const RAMP_S = 5;
const REPLAY_EVERY = 10;
// 5 calls back, about 60 ms at the peak: the comment replayed is often still
// queued or being handled, the inbox's hardest case.
const REPLAY_LAG = 5;
// The simulator's quantities (70 % one unit, 20 % two, 10 % three).
const QUANTITIES = [1, 1, 1, 1, 1, 1, 1, 2, 2, 3];
// Shapes of the simulator, which the parser reads without the LLM fallback.
const TEXTS = [
  "je prends {item}",
  "SOLD {item}",
  "{item} pour moi",
  "i'll take {item}",
  "j'achète {item}",
];

// The seed's (packages/livecart-core/.../application/demo_seed.py).
const LIVE_ID = "loadtest";
const PRODUCT_CODE = "Z99";
const STOCK = 200;
const SELLER_EMAIL = "demo-seller@livecart.test";

// The worker samples the queue's backlog at each reclaim, every 30 s, after a
// read that blocks for up to 2 s (services/worker/.../consumer.py): 40 s after
// the last call, every sample read was taken after it.
const DRAIN_SAMPLE_DELAY_S = 40;
// Then the time the workers get to empty the queue, past which queue_drained
// fails the run: long enough for a delivery a crashed handler left behind,
// reclaimed once idle for 120 s (QUEUE_RECLAIM_MIN_IDLE_MS), at a reclaim
// every 30 s.
const DRAIN_TIMEOUT_S = 180;

function positiveInteger(name: string, fallback: number): number {
  const value = Number(__ENV[name] ?? fallback);
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer`);
  return value;
}

// The rate's integral is the number of calls: two ramps of RAMP_S seconds
// make PEAK_RATE × RAMP_S, the hold the rest, plus one call against k6's
// rounding, which sends nothing.
const HOLD_MS = Math.round(((COMMENTS + 1) / PEAK_RATE - RAMP_S) * 1000);
if (HOLD_MS < 0) {
  throw new Error(`LOADTEST_COMMENTS must be at least LOADTEST_PEAK_RATE × ${String(RAMP_S)}`);
}

export const options = {
  scenarios: {
    comment_spike: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      // One VU per call in flight: calls of up to 1 s at the peak, then of up
      // to 3 s. Past that, k6 drops arrivals and webhook_calls fails the run,
      // which would no longer be the burst it claims to be.
      preAllocatedVUs: PEAK_RATE,
      maxVUs: 3 * PEAK_RATE,
      stages: [
        { target: PEAK_RATE, duration: `${String(RAMP_S)}s` },
        { target: PEAK_RATE, duration: `${String(HOLD_MS)}ms` },
        { target: 0, duration: `${String(RAMP_S)}s` },
      ],
    },
  },
  // Pass or fail is about correctness only. No latency threshold: the
  // generator and the stack share one laptop, and the summary keeps the
  // latencies for PERFORMANCE.md.
  thresholds: {
    // The claim, checked once every queued comment was handled.
    "checks{check:no_oversell}": ["rate==1"],
    "checks{check:queue_drained}": ["rate==1"],
    // The whole burst was sent.
    webhook_calls: [`count==${String(COMMENTS)}`],
    // 429 is abuse and 503 is load (ADR 0011), and neither belongs to this
    // scenario: it stays under the rate limit, and its comments fill 5 % of
    // QUEUE_MAX_DEPTH (20,000). Anything else (401, 422, a timeout) is a
    // broken run.
    webhook_rate_limited: ["count==0"],
    webhook_backpressure: ["count==0"],
    webhook_other: ["count==0"],
  },
  // The drain: its sample delay, its timeout, and the reads around them.
  teardownTimeout: "5m",
  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
};

const webhookCalls = new Counter("webhook_calls");
const accepted = new Counter("webhook_accepted");
const rateLimited = new Counter("webhook_rate_limited");
const backpressure = new Counter("webhook_backpressure");
const other = new Counter("webhook_other");
// The webhook's latency alone: http_req_duration also holds the sign-ins and
// the snapshot read of setup and teardown.
const webhookDuration = new Trend("webhook_duration");
// What teardown read, for the summary.
const stockTotal = new Gauge("loadtest_stock_total");
const stockReserved = new Gauge("loadtest_stock_reserved");
const stockSold = new Gauge("loadtest_stock_sold");
const orderCount = new Gauge("loadtest_orders");
const orderUnits = new Gauge("loadtest_order_units");

// --- The API's answers, as far as the scenario reads them ---------------------

interface LiveSession {
  id: string;
  external_live_id: string;
  status: string;
}

interface Product {
  id: string;
  code: string;
  variant: string;
  stock_total: number;
  reserved: number;
  sold: number;
}

interface Order {
  product_id: string;
  status: string;
  quantity: number;
}

/** The first event of the session's stream: its products and latest orders, in one read. */
interface Snapshot {
  products: Product[];
  orders: Order[];
  limit: number;
}

interface Target {
  sessionId: string;
  productId: string;
}

// --- The burst ----------------------------------------------------------------

/** The rank of the comment a call sends: its own, or a replay's, of REPLAY_LAG calls before. */
function rankOfCall(call: number): number {
  const original = call % REPLAY_EVERY === REPLAY_EVERY - 1 ? call - REPLAY_LAG : call;
  // The replays before it took no rank.
  return original - Math.floor(original / REPLAY_EVERY);
}

/** A comment derived from its rank alone, so that a replay sends the very same one. */
function comment(rank: number): Record<string, string> {
  const quantity = QUANTITIES[rank % QUANTITIES.length] ?? 1;
  const item = quantity === 1 ? PRODUCT_CODE : `${PRODUCT_CODE} x${String(quantity)}`;
  const number = String(rank).padStart(6, "0");
  return {
    platform: "simulator",
    external_comment_id: `loadtest-comment-${number}`,
    external_live_id: LIVE_ID,
    // One buyer per comment: the rule of one pending order per buyer and
    // product never turns a purchase down.
    author_platform_user_id: `loadtest-buyer-${number}`,
    text: (TEXTS[rank % TEXTS.length] ?? "{item}").replace("{item}", item),
    posted_at: new Date(exec.scenario.startTime + rank).toISOString(),
  };
}

export default function (): void {
  const call = exec.scenario.iterationInTest;
  if (call >= COMMENTS) return;
  const body = JSON.stringify({ comments: [comment(rankOfCall(call))] });
  const response = http.post(`${API_URL}/api/v1/webhooks/comments/simulator`, body, {
    headers: {
      "Content-Type": "application/json",
      [SIGNATURE_HEADER]: signature(WEBHOOK_SECRET, body, Math.floor(Date.now() / 1000)),
    },
    // The simulator's timeout. No retry: a retry would be an arrival the open
    // model did not plan; the counters say what each call got.
    timeout: "10s",
    tags: { name: "comment webhook" },
    responseCallback: http.expectedStatuses(202),
  });
  webhookCalls.add(1);
  webhookDuration.add(response.timings.duration);
  if (response.status === 202) accepted.add(1);
  else if (response.status === 429) rateLimited.add(1);
  else if (response.status === 503) backpressure.add(1);
  else other.add(1);
  check(response, {
    "accepted (202)": (r) => r.status === 202,
    "not rate limited (429)": (r) => r.status !== 429,
    "no backpressure (503)": (r) => r.status !== 503,
  });
}

// --- Before and after ---------------------------------------------------------

export function setup(): Target {
  if (WEBHOOK_SECRET === "" || SELLER_PASSWORD === "") {
    throw new Error("COMMENT_WEBHOOK_SECRET_SIMULATOR and SEED_DEMO_PASSWORD must be set");
  }
  if (queueBacklog() === null) throw new Error(`no queue backlog at ${METRICS_URL}`);
  const token = signIn();
  const sessions = (sellerGet("/api/v1/live-sessions?limit=100", token) as { items: LiveSession[] })
    .items;
  const session = sessions.find((s) => s.external_live_id === LIVE_ID && s.status === "live");
  if (session === undefined) throw new Error(`no live session ${LIVE_ID}: run the seed`);
  const products = (
    sellerGet(`/api/v1/live-sessions/${session.id}/products`, token) as { items: Product[] }
  ).items;
  const product = products.find((p) => p.code === PRODUCT_CODE && p.variant === "");
  if (product === undefined) throw new Error(`no ${PRODUCT_CODE} in ${LIVE_ID}: run the seed`);
  if (product.stock_total !== STOCK || product.reserved + product.sold !== 0) {
    throw new Error(`${LIVE_ID} is not as seeded: docker compose run --rm seed --reset-loadtest`);
  }
  // A first sample of each counter: k6 skips the thresholds of a metric
  // without any, which would let count==COMMENTS pass on no call at all.
  for (const counter of [webhookCalls, accepted, rateLimited, backpressure, other]) {
    counter.add(0);
  }
  return { sessionId: session.id, productId: product.id };
}

export function teardown(target: Target): void {
  check(waitForDrain(), { queue_drained: (drained) => drained });
  let problems: string[];
  try {
    problems = oversellProblems(readSnapshot(target.sessionId, signIn()), target.productId);
  } catch (error) {
    // Fail closed: a check that could not read the session never passes.
    problems = [`the session could not be read: ${String(error)}`];
  }
  for (const problem of problems) console.error(`no_oversell: ${problem}`);
  check(problems, { no_oversell: (found) => found.length === 0 });
}

/** Whether the comment queue's backlog (lag + pending) read 0 once every call had been answered. */
function waitForDrain(): boolean {
  sleep(DRAIN_SAMPLE_DELAY_S);
  const deadline = Date.now() + DRAIN_TIMEOUT_S * 1000;
  for (;;) {
    if (queueBacklog() === 0) return true;
    if (Date.now() >= deadline) return false;
    sleep(2);
  }
}

/** The worker's `livecart_queue_backlog`, or null when the endpoint does not give it. */
function queueBacklog(): number | null {
  const response = http.get(METRICS_URL, { timeout: "5s", tags: { name: "worker metrics" } });
  const sample = /^livecart_queue_backlog (\S+)$/m.exec(response.body ?? "");
  return sample?.[1] === undefined ? null : Number(sample[1]);
}

/** What contradicts "no unit promised twice", from one consistent read of stock and orders. */
function oversellProblems(snapshot: Snapshot, productId: string): string[] {
  const product = snapshot.products.find((p) => p.id === productId);
  if (product === undefined) return [`${PRODUCT_CODE} is not in the session`];
  stockTotal.add(product.stock_total);
  stockReserved.add(product.reserved);
  stockSold.add(product.sold);
  // The snapshot lists the latest `limit` orders: a full list may not be all of them.
  if (snapshot.orders.length >= snapshot.limit) {
    return [
      `${String(snapshot.orders.length)} orders listed: more may exist than the snapshot shows`,
    ];
  }
  const holding = snapshot.orders.filter(
    (o) => o.product_id === productId && (o.status === "pending_payment" || o.status === "paid"),
  );
  const units = holding.reduce((sum, o) => sum + o.quantity, 0);
  orderCount.add(holding.length);
  orderUnits.add(units);
  const problems: string[] = [];
  if (product.stock_total !== STOCK) {
    problems.push(`the stock moved during the run: ${String(product.stock_total)}`);
  }
  if (units > STOCK) problems.push(`the orders hold ${String(units)} units of ${String(STOCK)}`);
  if (units !== product.reserved + product.sold) {
    problems.push(
      `the orders hold ${String(units)} units, the counters ${String(product.reserved)} ` +
        `reserved and ${String(product.sold)} sold`,
    );
  }
  return problems;
}

function signIn(): string {
  const response = http.post(
    `${API_URL}/api/v1/auth/login`,
    JSON.stringify({ email: SELLER_EMAIL, password: SELLER_PASSWORD }),
    {
      // The login checks Origin (login CSRF): a client outside a browser sends it.
      headers: { "Content-Type": "application/json", Origin: API_URL },
      tags: { name: "login" },
    },
  );
  if (response.status !== 200) throw new Error(`login answered ${String(response.status)}`);
  return (response.json() as { access_token: string }).access_token;
}

function sellerGet(path: string, token: string): unknown {
  const response = http.get(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    tags: { name: "seller api" },
  });
  if (response.status !== 200) throw new Error(`${path} answered ${String(response.status)}`);
  return response.json();
}

/**
 * The session's snapshot, the first event of its stream, which the dashboard
 * reads too: the API lists a session's orders nowhere else. k6 has no
 * streaming client, so a timeout ends the request and k6 keeps what arrived
 * before it; k6 then logs the request as failed. Should a version of k6 keep
 * nothing, the parse fails, and so does no_oversell.
 */
function readSnapshot(sessionId: string, token: string): Snapshot {
  const response = http.get(`${API_URL}/api/v1/live-sessions/${sessionId}/events`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: "3s",
    tags: { name: "session snapshot" },
  });
  const event = /^event: snapshot\r?\ndata: (.+)$/m.exec(response.body ?? "");
  if (event?.[1] === undefined) throw new Error(`no snapshot in the stream (${response.error})`);
  return JSON.parse(event[1]) as Snapshot;
}

// --- The summary --------------------------------------------------------------

interface SummaryMetric {
  values: Record<string, number | undefined>;
  thresholds?: Record<string, { ok: boolean }>;
}

/**
 * Writes loadtest/results/comment-spike-<UTC time>.json, versioned as the
 * evidence of PERFORMANCE.md, and a short report on standard output.
 */
export function handleSummary(data: {
  metrics: Record<string, SummaryMetric | undefined>;
}): Record<string, string> {
  const value = (metric: string, stat: string): number | null =>
    data.metrics[metric]?.values[stat] ?? null;
  const finishedAt = new Date();
  const replays = Math.floor(COMMENTS / REPLAY_EVERY);
  const summary = {
    scenario: "comment-spike",
    finished_at: finishedAt.toISOString(),
    parameters: {
      calls: COMMENTS,
      comments: COMMENTS - replays,
      replays,
      peak_rate_per_s: PEAK_RATE,
      ramp_s: RAMP_S,
      product: PRODUCT_CODE,
      stock: STOCK,
    },
    responses: {
      "202": value("webhook_accepted", "count"),
      "429": value("webhook_rate_limited", "count"),
      "503": value("webhook_backpressure", "count"),
      other: value("webhook_other", "count"),
    },
    checks: {
      no_oversell: value("checks{check:no_oversell}", "rate") === 1,
      queue_drained: value("checks{check:queue_drained}", "rate") === 1,
    },
    stock: {
      total: value("loadtest_stock_total", "value"),
      reserved: value("loadtest_stock_reserved", "value"),
      sold: value("loadtest_stock_sold", "value"),
    },
    orders: {
      count: value("loadtest_orders", "value"),
      units: value("loadtest_order_units", "value"),
    },
    thresholds_ok: Object.values(data.metrics).every((metric) =>
      Object.values(metric?.thresholds ?? {}).every((threshold) => threshold.ok),
    ),
    // Sorted by name: k6 hands them over in no stable order.
    metrics: Object.fromEntries(
      Object.entries(data.metrics).sort(([a], [b]) => a.localeCompare(b)),
    ),
  };
  const stamp = finishedAt.toISOString().slice(0, 19).replace(/[-:]/g, "");
  const file = `${RESULTS_DIR}/comment-spike-${stamp}Z.json`;
  // Only a run that reached its check leaves a summary: one whose setup
  // refused the session (not reset, say) is evidence of nothing.
  const checked =
    (value("checks{check:no_oversell}", "passes") ?? 0) +
      (value("checks{check:no_oversell}", "fails") ?? 0) >
    0;
  const verdict = (ok: boolean): string => (ok ? "passed" : "FAILED");
  const ms = (stat: string): string => (value("webhook_duration", stat) ?? 0).toFixed(1);
  const report = [
    `comment spike: ${String(COMMENTS)} calls (${String(replays)} replays), peak ${String(PEAK_RATE)}/s`,
    `  responses: ${JSON.stringify(summary.responses)}`,
    `  webhook latency: med ${ms("med")} ms, p(95) ${ms("p(95)")} ms`,
    `  ${PRODUCT_CODE}: ${JSON.stringify(summary.stock)}, orders: ${JSON.stringify(summary.orders)}`,
    `  no_oversell ${verdict(summary.checks.no_oversell)}, queue_drained ${verdict(summary.checks.queue_drained)}, thresholds ${verdict(summary.thresholds_ok)}`,
    checked ? `  summary: ${file}` : "  no summary written: the run never reached no_oversell",
  ];
  const stdout = `${report.join("\n")}\n`;
  return checked ? { [file]: `${JSON.stringify(summary, null, 2)}\n`, stdout } : { stdout };
}
