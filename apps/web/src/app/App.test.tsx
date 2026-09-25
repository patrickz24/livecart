import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { createApiClient } from "../api/client";
import { App } from "./App";

afterEach(cleanup);

describe("App", () => {
  it("calls /health on its own origin and reports the API as up", async () => {
    const requested: string[] = [];
    const client = createApiClient({
      fetch: (input) => {
        requested.push(new Request(input).url);
        return Promise.resolve(Response.json({ status: "ok" }));
      },
    });

    render(<App client={client} />);

    await screen.findByText("API is up");
    expect(requested).toEqual([`${window.location.origin}/health`]);
  });

  it("reports the API as unreachable when the request fails", async () => {
    const client = createApiClient({
      fetch: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    render(<App client={client} />);

    expect((await screen.findByText("API is unreachable")).getAttribute("role")).toBe("status");
  });
});
