// Compose smoke test: starts the stack (building any missing image), waits for
// every healthcheck, then checks the state and the endpoints that a developer
// relies on after `docker compose up`. The stack is left running.
//
//   cp .env.example .env            # once
//   node scripts/smoke-compose.mjs  # or: pnpm run smoke:compose
//
// Keep EXPECTED_SERVICES and HTTP_CHECKS in step with compose.yaml: every
// service added to the stack is added here in the same commit.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { isMainModule } from "./cli.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const WAIT_TIMEOUT_S = 180;
const HTTP_TIMEOUT_MS = 5_000;

/** Final state of each compose service: "healthy", or "completed" for one-shot jobs. */
export const EXPECTED_SERVICES = {
  postgres: "healthy",
  redis: "healthy",
  migrate: "completed",
  api: "healthy",
  worker: "healthy",
  web: "healthy",
};

const isJson = (response) => response.headers.get("content-type")?.startsWith("application/json");

export const HTTP_CHECKS = [
  {
    name: "API readiness on 127.0.0.1:8000",
    url: "http://127.0.0.1:8000/ready",
    accept: (response, body) => response.status === 200 && JSON.parse(body).status === "ready",
  },
  {
    name: "nginx healthz",
    url: "http://127.0.0.1:8080/healthz",
    accept: (response) => response.status === 200,
  },
  {
    name: "API liveness through nginx",
    url: "http://127.0.0.1:8080/health",
    accept: (response, body) =>
      response.status === 200 && isJson(response) && JSON.parse(body).status === "ok",
  },
  {
    name: "dashboard page with its security headers",
    url: "http://127.0.0.1:8080/",
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

async function runHttpCheck(check) {
  try {
    const response = await fetch(check.url, { signal: AbortSignal.timeout(HTTP_TIMEOUT_MS) });
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
  } catch {
    // Keep going: the state report below says which service is not healthy.
    failures += 1;
  }

  const problems = serviceProblems(parseComposePs(compose("ps", "--all", "--format", "json")));
  for (const problem of problems) console.error(`FAIL  ${problem}`);
  if (problems.length === 0) {
    console.log(`ok    services: ${Object.keys(EXPECTED_SERVICES).join(", ")}`);
  }
  failures += problems.length;

  for (const check of HTTP_CHECKS) {
    const problem = await runHttpCheck(check);
    if (problem === null) {
      console.log(`ok    ${check.name}`);
    } else {
      console.error(`FAIL  ${check.name} (${check.url}): ${problem}`);
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
  await main();
}
