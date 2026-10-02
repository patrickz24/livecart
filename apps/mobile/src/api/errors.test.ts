import { describe, expect, it } from "@jest/globals";
import { getOrderDetails } from "@livecart/api-client";

import { FAKE_API_ORIGIN, FakeApi, json } from "../test/fakeApi";
import { tokenAt } from "../test/memoryStorage";
import { orderDetails } from "../test/orders";

import { createApiClient } from "./client";
import { ApiError, NetworkError, describeError, isRetryable, unwrap } from "./errors";

const DETAILS = "/api/v1/orders/LC-7K3F9Q/details";

/** What unwrap makes of the API's answer to GET /details, through the generated client. */
async function outcome(api: FakeApi): Promise<unknown> {
  const client = createApiClient(FAKE_API_ORIGIN, api.fetch);
  try {
    return await unwrap(
      getOrderDetails({ client, path: { reference: "LC-7K3F9Q" }, auth: tokenAt(1) }),
    );
  } catch (error) {
    return error;
  }
}

function answering(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new FakeApi().on("GET", DETAILS, json(status, body, headers));
}

describe("unwrap", () => {
  it("answers the data of a 2xx", async () => {
    expect(await outcome(answering(200, orderDetails()))).toEqual(orderDetails());
  });

  it("throws the status, the detail and the Retry-After of a 429", async () => {
    const error = await outcome(
      answering(429, { detail: "Too many requests" }, { "Retry-After": "30" }),
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      detail: "Too many requests",
      retryAfterSeconds: 30,
    });
  });

  it.each([
    ["no Retry-After", {}],
    ["an HTTP date", { "Retry-After": "Fri, 02 Oct 2026 18:00:00 GMT" }],
    ["a negative number", { "Retry-After": "-5" }],
  ])("reads no wait from a 503 with %s", async (_, headers: Record<string, string>) => {
    const error = await outcome(answering(503, { detail: "Unavailable" }, headers));

    expect(error).toMatchObject({ status: 503, retryAfterSeconds: null });
  });

  it("throws a NetworkError when no answer comes", async () => {
    const api = new FakeApi().on("GET", DETAILS, () => {
      throw new TypeError("Network request failed");
    });

    expect(await outcome(api)).toBeInstanceOf(NetworkError);
  });
});

describe("isRetryable", () => {
  it.each([400, 404, 409, 422, 429])(
    "never retries a %i: the API refused the request",
    (status) => {
      expect(isRetryable(new ApiError(status, null))).toBe(false);
    },
  );

  it.each([500, 502, 503])("retries a %i", (status) => {
    expect(isRetryable(new ApiError(status, null, 1))).toBe(true);
  });

  it("retries a request that got no answer, or any other failure", () => {
    expect(isRetryable(new NetworkError(new TypeError("Network request failed")))).toBe(true);
    expect(isRetryable(new Error("boom"))).toBe(true);
  });
});

describe("describeError", () => {
  it("says how long to wait after a 429, when the API said it", () => {
    expect(describeError(new ApiError(429, null, 30))).toBe(
      "Too many requests. Try again in 30 s.",
    );
    expect(describeError(new ApiError(429, null))).toBe(
      "Too many requests. Wait a moment, then try again.",
    );
  });

  it.each([500, 503])("says the server is unavailable after a %i", (status) => {
    expect(describeError(new ApiError(status, { detail: "Database unavailable" }, 1))).toBe(
      "The server is unavailable right now. Try again in a moment.",
    );
  });

  it("gives the API's detail of another refusal, or its status", () => {
    expect(describeError(new ApiError(409, { detail: "The order is already paid" }))).toBe(
      "The order is already paid",
    );
    // FastAPI's 422 has a list as its detail.
    expect(describeError(new ApiError(422, { detail: [{ msg: "Field required" }] }))).toBe(
      "The request was refused (422).",
    );
  });

  it("tells a server out of reach from any other failure", () => {
    expect(describeError(new NetworkError(new TypeError("Network request failed")))).toBe(
      "The server cannot be reached. Check your connection and try again.",
    );
    expect(describeError(new Error("boom"))).toBe("Something went wrong. Try again.");
  });
});
