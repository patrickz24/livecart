import { describe, expect, it, vi } from "vitest";

import {
  AuthSession,
  EXPIRY_MARGIN_MS,
  REFRESH_LOCK,
  type RefreshAnswer,
  type RefreshLocks,
} from "./session";

const granted = (accessToken: string, expiresInSeconds = 900): RefreshAnswer => ({
  kind: "granted",
  grant: { accessToken, expiresInSeconds },
});

/** A refresh request that answers only when the test says so. */
function deferredRefresh() {
  const pending: ((answer: RefreshAnswer) => void)[] = [];
  const request = vi.fn(
    () =>
      new Promise<RefreshAnswer>((resolve) => {
        pending.push(resolve);
      }),
  );
  return { request, answer: (answer: RefreshAnswer) => pending.shift()?.(answer) };
}

/** Web Locks between tabs of one origin: exclusive, granted in request order. */
class FakeLocks implements RefreshLocks {
  readonly names: string[] = [];
  #tail: Promise<unknown> = Promise.resolve();

  request(name: string, callback: () => Promise<RefreshAnswer>): Promise<RefreshAnswer> {
    this.names.push(name);
    const run = this.#tail.then(callback, callback);
    this.#tail = run.catch(() => undefined);
    return run;
  }
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("AuthSession.refresh", () => {
  it("shares one request between the callers of a tab", async () => {
    const refresh = deferredRefresh();
    const session = new AuthSession({ requestRefresh: refresh.request, locks: null });

    const calls = [session.refresh(), session.refresh(), session.validToken()];
    refresh.answer(granted("access-1"));

    expect(await Promise.all(calls)).toEqual(["access-1", "access-1", "access-1"]);
    expect(refresh.request).toHaveBeenCalledTimes(1);
    expect(session.status).toBe("signed_in");
    expect(session.accessToken()).toBe("access-1");
  });

  it("sends a new request once the previous one has answered", async () => {
    const answers: RefreshAnswer[] = [granted("access-1"), granted("access-2")];
    const requestRefresh = vi.fn(() =>
      Promise.resolve(answers.shift() ?? { kind: "refused" as const }),
    );
    const session = new AuthSession({ requestRefresh, locks: null });

    expect(await session.refresh()).toBe("access-1");
    expect(await session.refresh()).toBe("access-2");
    expect(requestRefresh).toHaveBeenCalledTimes(2);
  });

  it("lets one tab refresh at a time through the shared lock", async () => {
    const locks = new FakeLocks();
    let running = 0;
    let mostAtOnce = 0;
    const tab = (token: string) =>
      new AuthSession({
        locks,
        requestRefresh: async () => {
          running += 1;
          mostAtOnce = Math.max(mostAtOnce, running);
          await flush();
          running -= 1;
          return granted(token);
        },
      });
    const first = tab("access-first");
    const second = tab("access-second");

    const tokens = await Promise.all([first.refresh(), second.refresh(), first.refresh()]);

    // Each tab got its own token; the second only asked once the first was answered.
    expect(tokens).toEqual(["access-first", "access-second", "access-first"]);
    expect(mostAtOnce).toBe(1);
    expect(locks.names).toEqual([REFRESH_LOCK, REFRESH_LOCK]);
  });

  it("signs the session out when the server refuses the cookie", async () => {
    const answers: RefreshAnswer[] = [granted("access-1"), { kind: "refused" }];
    const session = new AuthSession({
      requestRefresh: () => Promise.resolve(answers.shift() ?? { kind: "refused" }),
      locks: null,
    });
    const listener = vi.fn();
    session.subscribe(listener);

    await session.refresh();
    expect(await session.refresh()).toBeNull();

    expect(session.status).toBe("signed_out");
    expect(session.endReason).toBe("expired");
    expect(session.accessToken()).toBeNull();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("gives no verdict when the server cannot be asked, and asks again next time", async () => {
    const requestRefresh = vi
      .fn<() => Promise<RefreshAnswer>>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(granted("access-1"));
    const session = new AuthSession({ requestRefresh, locks: null });

    await expect(session.refresh()).rejects.toThrow("Failed to fetch");
    expect(session.status).toBe("unknown");

    expect(await session.refresh()).toBe("access-1");
    expect(requestRefresh).toHaveBeenCalledTimes(2);
  });
});

describe("AuthSession.accessToken", () => {
  it("counts a token as expired within the margin before its expiry", () => {
    let now = 1_000_000;
    const session = new AuthSession({
      requestRefresh: () => Promise.resolve({ kind: "refused" }),
      locks: null,
      now: () => now,
    });
    session.signIn({ accessToken: "access-1", expiresInSeconds: 900 });

    now += 900_000 - EXPIRY_MARGIN_MS - 1;
    expect(session.accessToken()).toBe("access-1");
    now += 1;
    expect(session.accessToken()).toBeNull();
  });

  it("forgets the token on a logout, and says why", () => {
    const session = new AuthSession({
      requestRefresh: () => Promise.resolve({ kind: "refused" }),
      locks: null,
    });
    session.signIn({ accessToken: "access-1", expiresInSeconds: 900 });

    session.signOut("logged_out");

    expect(session.accessToken()).toBeNull();
    expect(session.status).toBe("signed_out");
    expect(session.endReason).toBe("logged_out");
  });
});
