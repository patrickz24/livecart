import type { AuthSession } from "./session";

const BEARER = "Bearer ";

/**
 * A `fetch` for the generated client: a request sent with an access token
 * that answers 401 is sent once more, with a refreshed token.
 *
 * The client's `auth` callback puts the token on the requests whose route
 * requires one; this wrapper only reacts to their 401. Several requests that
 * fail together share one refresh (`AuthSession.refresh`), and a request whose
 * token another one has already replaced is retried with the new token
 * without refreshing again. When the refresh is refused, or the server cannot
 * be reached, the 401 is answered as it came: the session, signed out in the
 * first case, takes the seller back to the login page.
 */
export function createAuthFetch(
  session: AuthSession,
  send: (request: Request) => Promise<Response>,
): typeof fetch {
  return async (input, init) => {
    const request = new Request(input, init);
    const sentWith = request.headers.get("Authorization");
    if (sentWith === null) {
      return send(request);
    }
    // The body can be read once: keep a copy for the retry.
    const retry = request.clone();
    const response = await send(request);
    if (response.status !== 401) {
      return response;
    }
    const token = await replacementToken(session, sentWith);
    if (token === null) {
      return response;
    }
    retry.headers.set("Authorization", `${BEARER}${token}`);
    return send(retry);
  };
}

async function replacementToken(session: AuthSession, sentWith: string): Promise<string | null> {
  const current = session.accessToken();
  if (current !== null && `${BEARER}${current}` !== sentWith) {
    return current;
  }
  try {
    return await session.refresh();
  } catch {
    // Unreachable: no verdict on the session, and nothing better to send.
    return null;
  }
}
