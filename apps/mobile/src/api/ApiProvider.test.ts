import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";

import { createQueryClient } from "./ApiProvider";
import { ApiError, NetworkError } from "./errors";

/** A query of the app's cache whose every attempt fails with `error`, and the attempts made. */
function failingQuery(error: Error) {
  const attempts: number[] = [];
  const query = createQueryClient().query({
    queryKey: ["failing"],
    queryFn: () => {
      attempts.push(Date.now());
      return Promise.reject(error);
    },
  });
  // Rejected only once the retries are over; awaited by each test.
  query.catch(() => undefined);
  return { attempts, query };
}

describe("the app's query cache", () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it.each([
    ["a 503", new ApiError(503, null)],
    ["a 500", new ApiError(500, null)],
    ["no answer", new NetworkError(new TypeError("Network request failed"))],
  ])("tries a query twice more after %s", async (_, error) => {
    const { attempts, query } = failingQuery(error);

    await jest.runAllTimersAsync();

    await expect(query).rejects.toBe(error);
    expect(attempts).toHaveLength(3);
  });

  it.each([404, 429])("never tries again after a %i", async (status) => {
    const error = new ApiError(status, null, 30);
    const { attempts, query } = failingQuery(error);

    await jest.runAllTimersAsync();

    await expect(query).rejects.toBe(error);
    expect(attempts).toHaveLength(1);
  });
});
