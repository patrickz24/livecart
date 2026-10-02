import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeApi, SELLER, signedInApi, tokenAnswer } from "../test/fakeApi";
import { renderApp } from "../test/renderApp";

const refused = () => Response.json({ detail: "Invalid refresh token" }, { status: 401 });

describe("auth flow", () => {
  it("sends a visitor without a refresh cookie to the login page", async () => {
    const api = new FakeApi().on("POST", "/api/v1/auth/refresh", refused);

    renderApp("/sessions", api);

    expect(await screen.findByRole("heading", { name: "Sign in to your dashboard" })).toBeTruthy();
    expect(api.sent("POST", "/api/v1/auth/refresh")).toHaveLength(1);
  });

  it("signs in, sends the token as a bearer, and returns to the page asked for", async () => {
    const api = new FakeApi()
      .on("POST", "/api/v1/auth/refresh", refused)
      .on("POST", "/api/v1/auth/login", tokenAnswer("access-1"))
      .on("GET", "/api/v1/auth/me", (request) =>
        request.headers.get("Authorization") === "Bearer access-1"
          ? Response.json(SELLER)
          : Response.json({ detail: "Not authenticated" }, { status: 401 }),
      );
    const { user, router } = renderApp("/sessions", api);

    await user.type(await screen.findByLabelText("E-mail"), "demo-seller@livecart.test");
    await user.type(screen.getByLabelText("Password"), "a-long-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Live sessions" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/sessions");
    expect(await screen.findByText("Demo Seller")).toBeTruthy();
    expect(api.sent("POST", "/api/v1/auth/login")[0]?.body).toEqual({
      email: "demo-seller@livecart.test",
      password: "a-long-password",
    });
    expect(api.sent("GET", "/api/v1/auth/me")[0]?.headers.get("Authorization")).toBe(
      "Bearer access-1",
    );
  });

  it("signs in from the refresh cookie alone, without the login page", async () => {
    const api = signedInApi("access-1");

    renderApp("/", api);

    expect(await screen.findByRole("heading", { name: "Live sessions" })).toBeTruthy();
    expect(api.sent("POST", "/api/v1/auth/login")).toHaveLength(0);
  });

  it("says that a login was refused", async () => {
    const api = new FakeApi()
      .on("POST", "/api/v1/auth/refresh", refused)
      .on("POST", "/api/v1/auth/login", () =>
        Response.json({ detail: "Invalid e-mail or password" }, { status: 401 }),
      );
    const { user } = renderApp("/login", api);

    await user.type(screen.getByLabelText("E-mail"), "demo-seller@livecart.test");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect((await screen.findByRole("alert")).textContent).toBe("Invalid e-mail or password.");
  });

  it("logs out: revokes the cookie's session and returns to the login page", async () => {
    const api = signedInApi().on(
      "POST",
      "/api/v1/auth/logout",
      new Response(null, { status: 204 }),
    );
    const { user, services } = renderApp("/sessions", api);

    await user.click(await screen.findByRole("button", { name: "Log out" }));

    expect(await screen.findByRole("heading", { name: "Sign in to your dashboard" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("You are signed out.");
    expect(api.sent("POST", "/api/v1/auth/logout")).toHaveLength(1);
    expect(services.session.accessToken()).toBeNull();
  });

  it("offers to try again when the server cannot be reached, without signing out", async () => {
    const api = new FakeApi().on(
      "POST",
      "/api/v1/auth/refresh",
      () => new Response("Bad Gateway", { status: 502 }),
    );
    const { user } = renderApp("/sessions", api);

    expect((await screen.findByRole("alert")).textContent).toContain("cannot be reached");
    api
      .on("POST", "/api/v1/auth/refresh", tokenAnswer())
      .on("GET", "/api/v1/auth/me", Response.json(SELLER));
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("heading", { name: "Live sessions" })).toBeTruthy();
  });
});
