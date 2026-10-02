import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { signedInApi } from "../../test/fakeApi";
import { aSession } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

describe("session list", () => {
  it("lists the sessions a page at a time, with their status", async () => {
    const first = aSession({ id: "0199a000-0000-7000-8000-0000000000a2", title: "Friday drop" });
    const older = aSession({ title: "Summer sale", status: "ended" });
    const api = signedInApi().on("GET", "/api/v1/live-sessions", (_request, url) =>
      url.searchParams.get("cursor") === "next-page"
        ? Response.json({ items: [older], next_cursor: null })
        : Response.json({ items: [{ ...first, status: "live" }], next_cursor: "next-page" }),
    );
    const { user } = renderApp("/sessions", api);

    const row = (await screen.findByRole("rowheader", { name: /Friday drop/ })).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Live")).toBeTruthy();
    expect(within(row as HTMLElement).getByRole("link", { name: "Edit Friday drop" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Show older sessions" }));

    expect(await screen.findByRole("rowheader", { name: /Summer sale/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show older sessions" })).toBeNull();
  });

  it("says when there is no session yet", async () => {
    const api = signedInApi().on(
      "GET",
      "/api/v1/live-sessions",
      Response.json({ items: [], next_cursor: null }),
    );

    renderApp("/sessions", api);

    expect(await screen.findByText(/No live sessions yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "New session" })).toBeTruthy();
  });
});
