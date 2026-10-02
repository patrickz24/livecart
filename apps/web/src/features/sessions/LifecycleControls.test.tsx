import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { signedInApi } from "../../test/fakeApi";
import { aProduct, aSession, SESSION_ID } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

const SESSION = `/api/v1/live-sessions/${SESSION_ID}`;

/** The edit page of a session the API holds as `status`; `moves` answers start and end. */
function sessionApi(status: "draft" | "live" | "ended") {
  let current = aSession({ status });
  const api = signedInApi()
    .on("GET", SESSION, () => Response.json(current))
    .on("GET", `${SESSION}/products`, () =>
      Response.json({ items: [aProduct()], next_cursor: null }),
    )
    .on("POST", `${SESSION}/start`, () => {
      current = aSession({ status: "live", started_at: "2026-10-02T10:00:00Z" });
      return Response.json(current);
    })
    .on("POST", `${SESSION}/end`, () => {
      current = aSession({ status: "ended", ended_at: "2026-10-02T11:00:00Z" });
      return Response.json(current);
    });
  return {
    api,
    /** The session moves on its own, as another tab would move it. */
    moveTo: (status: "live" | "ended") => {
      current = aSession({ status });
    },
  };
}

describe("lifecycle controls", () => {
  it("starts a draft, which then offers to end it", async () => {
    const { api } = sessionApi("draft");
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    await user.click(await screen.findByRole("button", { name: "Start live" }));

    expect(await screen.findByRole("button", { name: "End live" })).toBeTruthy();
    expect(screen.getByText("Live")).toBeTruthy();
    // Start live is gone: the focus is on the heading, not lost to the page.
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
    expect(api.sent("POST", `${SESSION}/start`)).toHaveLength(1);
    // Once live, the live id is locked in the form too.
    expect(screen.getByLabelText("Live id").hasAttribute("readonly")).toBe(true);
  });

  it("asks before ending a live, with the focus on the confirmation", async () => {
    const { api } = sessionApi("live");
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    await user.click(await screen.findByRole("button", { name: "End live" }));

    const confirm = screen.getByRole("button", { name: "End live now" });
    expect(document.activeElement).toBe(confirm);
    expect(api.sent("POST", `${SESSION}/end`)).toHaveLength(0);

    await user.click(confirm);

    expect(await screen.findByText("Ended")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /End live/ })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
    expect(api.sent("POST", `${SESSION}/end`)).toHaveLength(1);
  });

  it("keeps the focus on the page when an end fails and its confirmation closes", async () => {
    const { api } = sessionApi("live");
    api.on("POST", `${SESSION}/end`, new Response("Bad Gateway", { status: 502 }));
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    await user.click(await screen.findByRole("button", { name: "End live" }));
    await user.click(screen.getByRole("button", { name: "End live now" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "The server is unavailable right now. Try again in a moment.",
    );
    expect(screen.queryByRole("button", { name: "End live now" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
  });

  it("cancels the end with Escape, back on the End live button", async () => {
    const { api } = sessionApi("live");
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    await user.click(await screen.findByRole("button", { name: "End live" }));
    await user.keyboard("{Escape}");

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "End live" }));
    expect(api.sent("POST", `${SESSION}/end`)).toHaveLength(0);
  });

  it("explains a 409 of the machine and shows the session as the server holds it", async () => {
    const { api, moveTo } = sessionApi("draft");
    api.on("POST", `${SESSION}/start`, () =>
      Response.json({ detail: "The session is ended: it cannot become live" }, { status: 409 }),
    );
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);
    const start = await screen.findByRole("button", { name: "Start live" });
    moveTo("ended");

    await user.click(start);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "The live could not start: The session is ended: it cannot become live. Its status is shown again as the server holds it.",
    );
    expect(await screen.findByText("Ended")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start live" })).toBeNull();
  });
});
