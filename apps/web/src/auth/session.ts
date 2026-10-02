/**
 * The seller's sign-in, as one tab holds it.
 *
 * - The access token lives in this object only: never in localStorage,
 *   sessionStorage or a cookie a script can read, so a reload drops it and the
 *   next request trades the refresh cookie for a new one (ADR 0010, ADR 0017).
 * - The refresh cookie (`lc_refresh`, HttpOnly) is the browser's: this code
 *   never sees it, it only calls `POST /api/v1/auth/refresh`.
 * - One refresh at a time. Within a tab, concurrent callers share the request
 *   in flight. Across tabs, which share one cookie jar, the request runs under
 *   the Web Locks API (`navigator.locks`), so that a second tab only sends the
 *   cookie once the first has received its successor: no two tabs ever send
 *   the same refresh token, and the server's 30 s reuse grace stays a safety
 *   net rather than the normal path. Where Web Locks are missing (an insecure
 *   context, such as plain HTTP on a LAN address), the tab refreshes without
 *   the lock and relies on that grace.
 */

/** What a login or a refresh grants: an access token and its lifetime. */
export interface AccessGrant {
  accessToken: string;
  expiresInSeconds: number;
}

/**
 * The refresh endpoint's verdict: a new access token, or a refusal, after
 * which the seller signs in again. A refresh that could not be asked (network
 * error, 5xx while the API restarts) is no verdict: `requestRefresh` throws.
 */
export type RefreshAnswer = { kind: "granted"; grant: AccessGrant } | { kind: "refused" };

/** `unknown` until the first refresh or login has answered. */
export type AuthStatus = "unknown" | "signed_in" | "signed_out";

/**
 * Why a signed-in session ended, for the login page to say: the seller logged
 * out (and the API confirmed it, or could not be reached to), or the refresh
 * was refused (the family expired or was revoked).
 */
export type SignOutReason = "logged_out" | "logout_unconfirmed" | "expired";

/** The part of the Web Locks API the refresh needs; `navigator.locks` is one. */
export interface RefreshLocks {
  request(name: string, callback: () => Promise<RefreshAnswer>): Promise<RefreshAnswer>;
}

export interface AuthSessionOptions {
  requestRefresh: () => Promise<RefreshAnswer>;
  /** `navigator.locks` in a browser; `null` to refresh without a cross-tab lock. */
  locks: RefreshLocks | null;
  now?: () => number;
}

/** The lock every tab of the origin takes around its refresh request. */
export const REFRESH_LOCK = "livecart:auth-refresh";
/**
 * A token this close to its expiry counts as expired, so that a request never
 * leaves with a token that dies on the way, and an event stream, which the API
 * closes 5 s before the token's expiry, reconnects with a fresh one.
 */
export const EXPIRY_MARGIN_MS = 30_000;

/** The Web Locks API of this browser, or `null` where the context lacks it. */
export function browserLocks(): RefreshLocks | null {
  // `navigator.locks` is typed as always present; insecure contexts lack it.
  const locks = (navigator as Partial<Navigator>).locks;
  return locks ?? null;
}

export class AuthSession {
  readonly #requestRefresh: () => Promise<RefreshAnswer>;
  readonly #locks: RefreshLocks | null;
  readonly #now: () => number;
  readonly #listeners = new Set<() => void>();
  #status: AuthStatus = "unknown";
  #endReason: SignOutReason | null = null;
  #token: string | null = null;
  #expiresAt = 0;
  #inFlight: Promise<string | null> | null = null;

  constructor(options: AuthSessionOptions) {
    this.#requestRefresh = options.requestRefresh;
    this.#locks = options.locks;
    this.#now = options.now ?? Date.now;
  }

  get status(): AuthStatus {
    return this.#status;
  }

  /** Why the last signed-in session of this tab ended; `null` if none did. */
  get endReason(): SignOutReason | null {
    return this.#endReason;
  }

  /** The current access token, or `null` when there is none or it is about to expire. */
  accessToken(): string | null {
    if (this.#token === null || this.#now() >= this.#expiresAt - EXPIRY_MARGIN_MS) {
      return null;
    }
    return this.#token;
  }

  /** A token to send now: the current one, or a refreshed one; `null` once signed out. */
  async validToken(): Promise<string | null> {
    return this.accessToken() ?? (await this.refresh());
  }

  /** Record the grant of a login. */
  signIn(grant: AccessGrant): void {
    this.#endReason = null;
    this.#grant(grant);
  }

  /** Forget the access token in this tab; the logout request is the caller's. */
  signOut(reason: SignOutReason): void {
    if (this.#status === "signed_in") {
      this.#endReason = reason;
    }
    this.#token = null;
    this.#expiresAt = 0;
    this.#setStatus("signed_out");
  }

  /**
   * Trade the refresh cookie for a new access token: the token, or `null` when
   * the server refused (the session is then signed out). Rejects when the
   * server could not be asked, leaving the session as it was. Callers in this
   * tab share one request; tabs take turns through the lock.
   */
  refresh(): Promise<string | null> {
    this.#inFlight ??= this.#refreshOnce().finally(() => {
      this.#inFlight = null;
    });
    return this.#inFlight;
  }

  /** For `useSyncExternalStore`. */
  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  getStatus = (): AuthStatus => this.#status;

  async #refreshOnce(): Promise<string | null> {
    const answer = await (this.#locks === null
      ? this.#requestRefresh()
      : this.#locks.request(REFRESH_LOCK, this.#requestRefresh));
    if (answer.kind === "refused") {
      this.signOut("expired");
      return null;
    }
    this.#grant(answer.grant);
    return answer.grant.accessToken;
  }

  #grant(grant: AccessGrant): void {
    this.#token = grant.accessToken;
    this.#expiresAt = this.#now() + grant.expiresInSeconds * 1000;
    this.#setStatus("signed_in");
  }

  #setStatus(status: AuthStatus): void {
    if (status === this.#status) {
      return;
    }
    this.#status = status;
    for (const listener of this.#listeners) {
      listener();
    }
  }
}
