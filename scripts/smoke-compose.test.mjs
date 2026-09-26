import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { containerState, parseComposePs, runningJobs, serviceProblems } from "./smoke-compose.mjs";

const running = (service, health = "healthy", name = `livecart-${service}-1`) => ({
  Name: name,
  Service: service,
  State: "running",
  Health: health,
  ExitCode: 0,
});

describe("parseComposePs", () => {
  it("reads one JSON object per line", () => {
    const output = `${JSON.stringify(running("api"))}\n${JSON.stringify(running("web"))}\n`;
    assert.deepEqual(
      parseComposePs(output).map((container) => container.Service),
      ["api", "web"],
    );
  });

  it("reads a JSON array, as older compose versions print", () => {
    const output = JSON.stringify([running("api")]);
    assert.equal(parseComposePs(output)[0]?.Service, "api");
  });

  it("returns no container for empty output", () => {
    assert.deepEqual(parseComposePs("\n"), []);
  });
});

describe("containerState", () => {
  it("reports a one-shot job that exited with 0 as completed", () => {
    assert.equal(containerState({ State: "exited", ExitCode: 0, Health: "" }), "completed");
  });

  it("reports a failed job with its exit code", () => {
    assert.equal(containerState({ State: "exited", ExitCode: 1 }), "exited with code 1");
  });

  it("prefers the health status of a running container", () => {
    assert.equal(containerState(running("api", "starting")), "starting");
    assert.equal(containerState({ State: "running", Health: "" }), "running");
  });
});

describe("serviceProblems", () => {
  const expected = { migrate: "completed", api: "healthy", worker: "healthy" };
  const migrate = { Name: "livecart-migrate-1", Service: "migrate", State: "exited", ExitCode: 0 };

  it("accepts a stack in the expected state, with scaled replicas", () => {
    const containers = [
      migrate,
      running("api"),
      running("worker", "healthy", "livecart-worker-1"),
      running("worker", "healthy", "livecart-worker-2"),
    ];
    assert.deepEqual(serviceProblems(containers, expected), []);
  });

  it("reports missing, unhealthy and uncovered services", () => {
    const containers = [migrate, running("api", "unhealthy"), running("simulator")];
    assert.deepEqual(serviceProblems(containers, expected), [
      "livecart-api-1: unhealthy, expected healthy",
      "worker: no container",
      "simulator: not covered by this smoke test",
    ]);
  });
});

describe("runningJobs", () => {
  const expected = { migrate: "completed", seed: "completed", api: "healthy" };
  const exited = (service) => ({
    Name: `livecart-${service}-1`,
    Service: service,
    State: "exited",
    ExitCode: 0,
  });

  it("names the one-shot jobs that have not exited yet", () => {
    const containers = [exited("migrate"), running("seed", ""), running("api")];
    assert.deepEqual(runningJobs(containers, expected), ["livecart-seed-1"]);
  });

  it("is empty once every job has exited, whatever its code", () => {
    const failed = { ...exited("seed"), ExitCode: 1 };
    assert.deepEqual(runningJobs([exited("migrate"), failed, running("api")], expected), []);
  });
});
