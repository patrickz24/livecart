import { describe, expect, it, vi } from "vitest";

import { createAuthFetch } from "./authFetch";
import { AuthSession, type RefreshAnswer } from "./session";

const URL_ME = "http://localhost/api/v1/auth/me";

function signedIn(token: string, refresh: () => Promise<RefreshAnswer>) {
  const session = new AuthSession({ requestRefresh: refresh, locks: null });
  session.signIn({ accessToken: token, expiresInSeconds: 900 });
  return session;
}

/** An API that accepts one token only, recording what each request carried. */
function apiAccepting(token: string) {
  const seen: { auth: string | null; body: string }[] = [];
  const send = vi.fn(async (request: Request) => {
    seen.push({ auth: request.headers.get("Authorization"), body: await request.text() });
    return request.headers.get("Authorization") === `Bearer ${token}`
      ? Response.json({ ok: true })
      : Response.json({ detail: "Invalid or expired access token" }, { status: 401 });
  });
  return { send, seen };
}

describe("createAuthFetch", () => {
  it("sends a request without a token as it is", async () => {
    const refresh = vi.fn(() => Promise.resolve<RefreshAnswer>({ kind: "refused" }));
    const api = apiAccepting("new");
    const authFetch = createAuthFetch(signedIn("old", refresh), api.send);

    const response = await authFetch("http://localhost/api/v1/auth/refresh", { method: "POST" });

    expect(response.status).toBe(401);
    expect(refresh).not.toHaveBeenCalled();
    expect(api.send).toHaveBeenCalledTimes(1);
  });

  it("retries the requests that failed together after one refresh, bodies intact", async () => {
    const refresh = vi.fn(() =>
      Promise.resolve<RefreshAnswer>({
        kind: "granted",
        grant: { accessToken: "new", expiresInSeconds: 900 },
      }),
    );
    const api = apiAccepting("new");
    const authFetch = createAuthFetch(signedIn("old", refresh), api.send);
    const post = (body: string) =>
      authFetch("http://localhost/api/v1/live-sessions", {
        method: "POST",
        headers: { Authorization: "Bearer old", "Content-Type": "application/json" },
        body,
      });

    const responses = await Promise.all([post('{"n":1}'), post('{"n":2}')]);

    expect(responses.map((response) => response.status)).toEqual([200, 200]);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(api.seen.filter((request) => request.auth === "Bearer new")).toEqual([
      { auth: "Bearer new", body: '{"n":1}' },
      { auth: "Bearer new", body: '{"n":2}' },
    ]);
  });

  it("uses the token another request has already refreshed", async () => {
    const refresh = vi.fn(() => Promise.resolve<RefreshAnswer>({ kind: "refused" }));
    const session = signedIn("old", refresh);
    session.signIn({ accessToken: "new", expiresInSeconds: 900 });
    const api = apiAccepting("new");

    const response = await createAuthFetch(session, api.send)(URL_ME, {
      headers: { Authorization: "Bearer old" },
    });

    expect(response.status).toBe(200);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("answers the 401 when the refresh is refused, and signs the session out", async () => {
    const session = signedIn("old", () => Promise.resolve({ kind: "refused" }));
    const api = apiAccepting("never");

    const response = await createAuthFetch(session, api.send)(URL_ME, {
      headers: { Authorization: "Bearer old" },
    });

    expect(response.status).toBe(401);
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(session.status).toBe("signed_out");
  });

  it("answers the 401 when the refresh cannot be asked, and keeps the session", async () => {
    const session = signedIn("old", () => Promise.reject(new TypeError("Failed to fetch")));
    const api = apiAccepting("never");

    const response = await createAuthFetch(session, api.send)(URL_ME, {
      headers: { Authorization: "Bearer old" },
    });

    expect(response.status).toBe(401);
    expect(session.status).toBe("signed_in");
  });
});
