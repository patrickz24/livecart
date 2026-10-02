import { refreshAccessToken, type AccessTokenResponse, type Client } from "@livecart/api-client";

import type { AccessGrant, RefreshAnswer } from "./session";

/** The refresh could not be asked: no answer, or a 5xx (nginx's 502 while the API restarts). */
export class RefreshUnavailable extends Error {
  constructor(status: number | null) {
    super(status === null ? "The refresh got no answer" : `The refresh answered ${String(status)}`);
    this.name = "RefreshUnavailable";
  }
}

export function grantOf(token: AccessTokenResponse): AccessGrant {
  return { accessToken: token.access_token, expiresInSeconds: token.expires_in };
}

/**
 * `POST /api/v1/auth/refresh`, which sends the HttpOnly cookie by itself: a
 * grant; a refusal on 401 (the cookie is missing, expired, reused or revoked,
 * and the API clears it) or 403 (the Origin check); a throw otherwise.
 */
export async function requestRefresh(client: Client): Promise<RefreshAnswer> {
  const { data, response } = await refreshAccessToken({ client });
  if (data !== undefined && response?.ok === true) {
    return { kind: "granted", grant: grantOf(data) };
  }
  if (response?.status === 401 || response?.status === 403) {
    return { kind: "refused" };
  }
  throw new RefreshUnavailable(response?.status ?? null);
}
