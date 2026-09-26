import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  containerState,
  EXPECTED_SERVICES,
  HTTP_CHECKS,
  parseComposePs,
  PUBLISHED_PORTS,
  publishedBaseUrl,
  serviceProblems,
} from "./smoke-compose.mjs";

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
    const containers = [migrate, running("api", "unhealthy"), running("k6")];
    assert.deepEqual(serviceProblems(containers, expected), [
      "livecart-api-1: unhealthy, expected healthy",
      "worker: no container",
      "k6: not covered by this smoke test",
    ]);
  });

  it("expects the simulator running, since it has no healthcheck", () => {
    const simulator = { ...running("simulator"), Health: "" };
    assert.deepEqual(serviceProblems([simulator], { simulator: EXPECTED_SERVICES.simulator }), []);
    assert.deepEqual(
      serviceProblems([{ ...simulator, State: "restarting" }], { simulator: "running" }),
      ["livecart-simulator-1: restarting, expected running"],
    );
  });
});

describe("publishedBaseUrl", () => {
  it("reaches a loopback port where compose published it", () => {
    assert.equal(publishedBaseUrl("127.0.0.1:8000\n"), "http://127.0.0.1:8000");
  });

  it("reaches a port published on every interface on the loopback address", () => {
    assert.equal(publishedBaseUrl("0.0.0.0:18080\r\n"), "http://127.0.0.1:18080");
    assert.equal(publishedBaseUrl("[::]:8080"), "http://127.0.0.1:8080");
  });

  it("takes the first line when a port is published on several addresses", () => {
    assert.equal(publishedBaseUrl("0.0.0.0:8080\n[::]:8080\n"), "http://127.0.0.1:8080");
  });

  it("is null when nothing is published", () => {
    assert.equal(publishedBaseUrl(""), null);
    assert.equal(publishedBaseUrl(":0"), null);
  });
});

describe("HTTP_CHECKS", () => {
  it("reach only services whose port the smoke test looks up, and that it expects healthy", () => {
    for (const check of HTTP_CHECKS) {
      assert.ok(check.service in PUBLISHED_PORTS, check.name);
      assert.equal(EXPECTED_SERVICES[check.service], "healthy", check.name);
      assert.ok(check.path.startsWith("/"), check.name);
    }
  });
});
