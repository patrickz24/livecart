// Compose smoke test: starts the stack (building any missing image) with
// `docker compose up --wait`, which must succeed on its own, then checks the
// state and the endpoints that a developer relies on after `docker compose up`.
// The stack is left running.
//
//   cp .env.example .env            # once
//   node scripts/smoke-compose.mjs  # or: pnpm run smoke:compose
//
// It runs against whichever compose project the environment names
// (COMPOSE_PROJECT_NAME, or `name:` in compose.yaml) and asks compose where the
// API and nginx are published, so a second, throwaway project with other host
// ports (a compose.override.yaml) is checked the same way as the dev stack.
//
// Keep EXPECTED_SERVICES and HTTP_CHECKS in step with compose.yaml: every
// service added to the stack is added here in the same commit.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { isMainModule, requirePinnedNode } from "./cli.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const WAIT_TIMEOUT_S = 180;
const HTTP_TIMEOUT_MS = 5_000;

/**
 * Final state of each compose service: "healthy", or "completed" for one-shot
 * jobs. `up --wait` accepts a job's exit only when another service depends on
 * its completion (service_completed_successfully): the API and the worker on
 * migrate, the worker on seed. A job nothing waits for fails `up --wait`.
 */
export const EXPECTED_SERVICES = {
  postgres: "healthy",
  redis: "healthy",
  migrate: "completed",
  seed: "completed",
  api: "healthy",
  worker: "healthy",
  web: "healthy",
};

/** The published ports the checks reach, by service: the container port of each. */
export const PUBLISHED_PORTS = { api: 8000, web: 8080 };

const isJson = (response) => response.headers.get("content-type")?.startsWith("application/json");

export const HTTP_CHECKS = [
  {
    name: "API readiness on its loopback port",
    service: "api",
    path: "/ready",
    accept: (response, body) => response.status === 200 && JSON.parse(body).status === "ready",
  },
  {
    name: "nginx healthz",
    service: "web",
    path: "/healthz",
    accept: (response) => response.status === 200,
  },
  {
    name: "API liveness through nginx",
    service: "web",
    path: "/health",
    accept: (response, body) =>
      response.status === 200 && isJson(response) && JSON.parse(body).status === "ok",
  },
  {
    name: "API security headers through nginx, the API's only",
    service: "web",
    path: "/health",
    // fetch joins repeated headers with ", ": a second CSP would show here.
    accept: (response) =>
      response.status === 200 &&
      response.headers.get("content-security-policy") ===
        "default-src 'none'; frame-ancestors 'none'" &&
      response.headers.get("x-frame-options") === "DENY" &&
      response.headers.get("referrer-policy") === "no-referrer",
  },
  {
    name: "demo storefront through nginx, seeded live with its seven products",
    service: "web",
    path: "/api/v1/storefronts/demo-seller",
    accept: (response, body) => {
      if (response.status !== 200) return false;
      const session = JSON.parse(body).live_session;
      const codes = new Set(session?.products.map((product) => product.code));
      return session?.title === "Friday drop" && codes.size === 7;
    },
  },
  {
    name: "dashboard page with its security headers",
    service: "web",
    path: "/",
    accept: (response) =>
      response.status === 200 &&
      response.headers.get("content-type")?.startsWith("text/html") === true &&
      response.headers.get("content-security-policy")?.includes("default-src 'self'") === true &&
      response.headers.get("x-content-type-options") === "nosniff",
  },
];

/** Parse `docker compose ps --format json`: one object per line, or a JSON array. */
export function parseComposePs(output) {
  const text = output.trim();
  if (text === "") return [];
  if (text.startsWith("[")) return JSON.parse(text);
  return text.split(/\r?\n/).map((line) => JSON.parse(line));
}

/** The observed state of one container, in the vocabulary of EXPECTED_SERVICES. */
export function containerState(container) {
  if (container.State === "exited") {
    return container.ExitCode === 0 ? "completed" : `exited with code ${container.ExitCode}`;
  }
  return container.Health || container.State;
}

/**
 * The base URL of a published port, from the output of `docker compose port`
 * ("127.0.0.1:8000", "0.0.0.0:18080" or "[::]:8080"). A port published on every
 * interface is reached on the loopback address; null when nothing is published.
 */
export function publishedBaseUrl(portOutput) {
  const line = portOutput.trim().split(/\r?\n/)[0] ?? "";
  const match = /^(\[[0-9a-f:.]*\]|[^:]+):(\d+)$/i.exec(line);
  if (match === null) return null;
  const [, host, port] = match;
  const wildcard = host === "0.0.0.0" || host === "[::]";
  return `http://${wildcard ? "127.0.0.1" : host}:${port}`;
}

/** Problems found in the containers, compared with the expected services. */
export function serviceProblems(containers, expected = EXPECTED_SERVICES) {
  const problems = [];
  for (const [service, wanted] of Object.entries(expected)) {
    const replicas = containers.filter((container) => container.Service === service);
    if (replicas.length === 0) {
      problems.push(`${service}: no container`);
    }
    for (const container of replicas) {
      const state = containerState(container);
      if (state !== wanted) problems.push(`${container.Name}: ${state}, expected ${wanted}`);
    }
  }
  const unexpected = containers.filter((container) => !(container.Service in expected));
  for (const container of unexpected) {
    problems.push(`${container.Service}: not covered by this smoke test`);
  }
  return problems;
}

function compose(...args) {
  return execFileSync("docker", ["compose", ...args], {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
}

function baseUrls() {
  const urls = {};
  for (const [service, port] of Object.entries(PUBLISHED_PORTS)) {
    try {
      urls[service] = publishedBaseUrl(compose("port", service, String(port)));
    } catch {
      urls[service] = null;
    }
  }
  return urls;
}

async function runHttpCheck(check, base) {
  if (base === null) return `${check.service} publishes no port ${PUBLISHED_PORTS[check.service]}`;
  try {
    const response = await fetch(`${base}${check.path}`, {
      signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
    });
    const body = await response.text();
    return check.accept(response, body) ? null : `HTTP ${response.status}: ${body.slice(0, 200)}`;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

async function main() {
  console.log(`Starting the stack (docker compose up -d --wait, up to ${WAIT_TIMEOUT_S} s)...`);
  let failures = 0;
  try {
    compose("up", "--detach", "--wait", "--wait-timeout", String(WAIT_TIMEOUT_S));
    console.log("ok    docker compose up --wait");
  } catch {
    // Keep going: the state report below says which service is not healthy.
    console.error("FAIL  docker compose up --wait exited with an error");
    failures += 1;
  }

  const problems = serviceProblems(parseComposePs(compose("ps", "--all", "--format", "json")));
  for (const problem of problems) console.error(`FAIL  ${problem}`);
  if (problems.length === 0) {
    console.log(`ok    services: ${Object.keys(EXPECTED_SERVICES).join(", ")}`);
  }
  failures += problems.length;

  const urls = baseUrls();
  for (const check of HTTP_CHECKS) {
    const base = urls[check.service];
    const problem = await runHttpCheck(check, base);
    if (problem === null) {
      console.log(`ok    ${check.name} (${base}${check.path})`);
    } else {
      console.error(`FAIL  ${check.name} (${base ?? check.service}${check.path}): ${problem}`);
      failures += 1;
    }
  }

  if (failures > 0) {
    console.error("\nCompose smoke test failed; inspect the stack with `docker compose logs`.");
    process.exit(1);
  }
  console.log("\nCompose smoke test passed.");
}

// Run only as a command, not when the tests import the helpers above.
if (isMainModule(import.meta.url)) {
  requirePinnedNode();
  await main();
}
