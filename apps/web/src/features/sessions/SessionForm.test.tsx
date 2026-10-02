import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { signedInApi } from "../../test/fakeApi";
import { aProduct, aSession, SESSION_ID, validationError } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

const SESSIONS = "/api/v1/live-sessions";
const PRODUCTS = `${SESSIONS}/${SESSION_ID}/products`;

function product(number: number) {
  return within(screen.getByRole("group", { name: `Product ${String(number)}` }));
}

/** The error a field announces with it (aria-describedby). */
function errorOf(field: HTMLElement): string {
  const ids = field.getAttribute("aria-describedby")?.split(" ") ?? [];
  return ids
    .filter((id) => id.endsWith("-error"))
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

function editableApi(session = aSession(), products = [aProduct()]) {
  return signedInApi()
    .on("GET", `${SESSIONS}/${SESSION_ID}`, () => Response.json(session))
    .on("GET", PRODUCTS, () => Response.json({ items: products, next_cursor: null }));
}

describe("session form", () => {
  it("checks every field before sending anything, and focuses the first error", async () => {
    const api = signedInApi();
    const { user } = renderApp("/sessions/new", api);

    await user.click(await screen.findByRole("button", { name: "Create session" }));

    const title = screen.getByLabelText("Title");
    expect(errorOf(title)).toBe("Enter a title.");
    expect(title.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(title);
    expect(errorOf(screen.getByLabelText("Live id"))).toBe("Enter the live's id on its platform.");
    expect(errorOf(product(1).getByLabelText("Code"))).toBe(
      "1 to 3 letters, then 1 to 4 digits: A12.",
    );
    expect(errorOf(product(1).getByLabelText("Name"))).toBe("Enter a name.");
    expect(errorOf(product(1).getByLabelText("Price (EUR)"))).toBe("Enter a price such as 19.90.");
    expect(api.sent("POST", SESSIONS)).toHaveLength(0);
  });

  it("refuses two rows with the same code and variant", async () => {
    const api = signedInApi();
    const { user } = renderApp("/sessions/new", api);
    await user.type(await screen.findByLabelText("Title"), "Friday drop");
    await user.type(screen.getByLabelText("Live id"), "friday-drop");
    for (const number of [1, 2]) {
      if (number === 2) {
        await user.click(screen.getByRole("button", { name: "Add a product" }));
      }
      await user.type(product(number).getByLabelText("Code"), "a12");
      await user.type(product(number).getByLabelText("Name"), "Shirt");
      await user.type(product(number).getByLabelText("Price (EUR)"), "10");
    }

    await user.click(screen.getByRole("button", { name: "Create session" }));

    expect(errorOf(product(2).getByLabelText("Code"))).toBe(
      "Another row has this code and variant.",
    );
    expect(api.sent("POST", SESSIONS)).toHaveLength(0);
  });

  it("creates the session, then its products with the generated bodies", async () => {
    const api = signedInApi()
      .on("POST", SESSIONS, () => Response.json(aSession(), { status: 201 }))
      .on("POST", PRODUCTS, () => Response.json(aProduct(), { status: 201 }))
      .on("GET", `${SESSIONS}/${SESSION_ID}`, () => Response.json(aSession()))
      .on("GET", PRODUCTS, () => Response.json({ items: [aProduct()], next_cursor: null }));
    const { user, router } = renderApp("/sessions/new", api);

    await user.type(await screen.findByLabelText("Title"), "  Friday drop ");
    await user.selectOptions(screen.getByLabelText("Platform"), "simulator");
    await user.type(screen.getByLabelText("Live id"), "friday-drop");
    await user.type(product(1).getByLabelText("Code"), " a12 ");
    await user.type(product(1).getByLabelText("Variant"), "M");
    await user.type(product(1).getByLabelText("Name"), "Linen shirt");
    await user.type(product(1).getByLabelText("Price (EUR)"), "29,9");
    await user.clear(product(1).getByLabelText("Stock"));
    await user.type(product(1).getByLabelText("Stock"), "10");
    await user.click(screen.getByRole("button", { name: "Create session" }));

    expect((await screen.findByText("Session created.")).getAttribute("role")).toBe("status");
    expect(router.state.location.pathname).toBe(`/sessions/${SESSION_ID}/edit`);
    expect(api.sent("POST", SESSIONS)[0]?.body).toEqual({
      title: "Friday drop",
      platform: "simulator",
      external_live_id: "friday-drop",
    });
    expect(api.sent("POST", PRODUCTS)[0]?.body).toEqual({
      code: "A12",
      variant: "M",
      name: "Linen shirt",
      price_minor: 2990,
      stock_total: 10,
    });
  });

  it("shows a 422 on the field it names, and does not create the session twice", async () => {
    let productAttempts = 0;
    const api = signedInApi()
      .on("POST", SESSIONS, () => Response.json(aSession(), { status: 201 }))
      .on("POST", PRODUCTS, () => {
        productAttempts += 1;
        return productAttempts === 1
          ? validationError("code", "Value error, 'Q12345' is not a product code")
          : Response.json(aProduct(), { status: 201 });
      })
      .on("GET", `${SESSIONS}/${SESSION_ID}`, () => Response.json(aSession()))
      .on("GET", PRODUCTS, () => Response.json({ items: [aProduct()], next_cursor: null }));
    const { user } = renderApp("/sessions/new", api);
    await user.type(await screen.findByLabelText("Title"), "Friday drop");
    await user.type(screen.getByLabelText("Live id"), "friday-drop");
    await user.type(product(1).getByLabelText("Code"), "A12");
    await user.type(product(1).getByLabelText("Name"), "Linen shirt");
    await user.type(product(1).getByLabelText("Price (EUR)"), "29.90");

    await user.click(screen.getByRole("button", { name: "Create session" }));

    const code = product(1).getByLabelText("Code");
    await expect.poll(() => errorOf(code)).toBe("'Q12345' is not a product code");
    expect(document.activeElement).toBe(code);

    await user.click(screen.getByRole("button", { name: "Create session" }));

    expect((await screen.findByText("Session created.")).getAttribute("role")).toBe("status");
    expect(api.sent("POST", SESSIONS)).toHaveLength(1);
    expect(api.sent("POST", PRODUCTS)).toHaveLength(2);
  });

  it("moves the focus to a neighbouring row, then to Add a product, as rows are removed", async () => {
    const { user } = renderApp("/sessions/new", signedInApi());
    const add = await screen.findByRole("button", { name: "Add a product" });
    await user.click(add);
    await user.click(add);
    for (const [number, code] of [
      [1, "A1"],
      [2, "B2"],
      [3, "C3"],
    ] as const) {
      await user.type(product(number).getByLabelText("Code"), code);
    }

    // The last row: the focus goes to the row before.
    await user.click(product(3).getByRole("button", { name: "Remove product 3" }));
    expect(document.activeElement).toBe(product(2).getByLabelText("Code"));
    expect(product(2).getByLabelText<HTMLInputElement>("Code").value).toBe("B2");

    // The first row: the focus goes to the row that takes its place.
    await user.click(product(1).getByRole("button", { name: "Remove product 1" }));
    expect(document.activeElement).toBe(product(1).getByLabelText("Code"));
    expect(product(1).getByLabelText<HTMLInputElement>("Code").value).toBe("B2");

    // The only row: the focus goes to the button that adds one again.
    await user.click(product(1).getByRole("button", { name: "Remove product 1" }));
    expect(screen.getByText("No products yet.")).toBeTruthy();
    expect(document.activeElement).toBe(add);
  });

  it("shows a taken live id on its field", async () => {
    const api = signedInApi().on("POST", SESSIONS, () =>
      Response.json({ detail: "Another session has this platform and live id" }, { status: 409 }),
    );
    const { user } = renderApp("/sessions/new", api);
    await user.type(await screen.findByLabelText("Title"), "Friday drop");
    await user.type(screen.getByLabelText("Live id"), "friday-drop");
    await user.click(product(1).getByRole("button", { name: "Remove product 1" }));

    await user.click(screen.getByRole("button", { name: "Create session" }));

    const liveId = screen.getByLabelText("Live id");
    await expect.poll(() => errorOf(liveId)).toBe("Another session has this platform and live id");
  });

  it("sends only what changed, and retries a restock with the same Idempotency-Key", async () => {
    let restocks = 0;
    const api = editableApi(aSession({ status: "live" })).on(
      "POST",
      `${PRODUCTS}/${aProduct().id}/restock`,
      () => {
        restocks += 1;
        return restocks === 1
          ? new Response("Bad Gateway", { status: 502 })
          : Response.json(aProduct({ stock_total: 15, available: 12 }));
      },
    );
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    const restock = await screen.findByLabelText("Restock");
    await user.type(restock, "5");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "The server is unavailable right now. Try again in a moment.",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect((await screen.findByText("Changes saved.")).getAttribute("role")).toBe("status");
    const [first, second] = api.sent("POST", `${PRODUCTS}/${aProduct().id}/restock`);
    expect(first?.body).toEqual({ delta: 5 });
    expect(first?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f]{32}$/);
    expect(second?.headers.get("Idempotency-Key")).toBe(first?.headers.get("Idempotency-Key"));
    // Nothing else changed: no PATCH of the session or of the product.
    expect(api.requests.filter((request) => request.method === "PATCH")).toHaveLength(0);
  });

  it("shows a restock below the units reserved and sold on its row", async () => {
    const api = editableApi(aSession({ status: "live" })).on(
      "POST",
      `${PRODUCTS}/${aProduct().id}/restock`,
      Response.json(
        { detail: "The stock total would fall below the units reserved and sold" },
        { status: 409 },
      ),
    );
    const { user } = renderApp(`/sessions/${SESSION_ID}/edit`, api);

    const restock = await screen.findByLabelText("Restock");
    await user.type(restock, "-9");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await expect
      .poll(() => errorOf(restock))
      .toBe("The stock total would fall below the units reserved and sold");
  });

  it("keeps what a live session locks read-only, and an ended one entirely", async () => {
    renderApp(`/sessions/${SESSION_ID}/edit`, editableApi(aSession({ status: "live" })));

    expect((await screen.findByLabelText("Live id")).hasAttribute("readonly")).toBe(true);
    expect(screen.getByLabelText("Platform").hasAttribute("readonly")).toBe(true);
    expect(product(1).getByLabelText("Code").hasAttribute("readonly")).toBe(true);
    expect(product(1).getByLabelText("Name").hasAttribute("readonly")).toBe(false);
  });

  it("offers no save for an ended session", async () => {
    renderApp(`/sessions/${SESSION_ID}/edit`, editableApi(aSession({ status: "ended" })));

    const notice = await screen.findByText("This session has ended: it can no longer change.");
    expect(notice.getAttribute("role")).toBe("status");
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(screen.getByLabelText("Title").hasAttribute("readonly")).toBe(true);
  });
});
