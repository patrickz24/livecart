/**
 * An in-memory API for the component tests: the `fetch` the services send
 * through, with handlers by method and path, and every request recorded.
 */
export interface RecordedRequest {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

type Handler = (request: Request, url: URL) => Response | Promise<Response>;

interface Route {
  method: string;
  path: string | RegExp;
  handler: Handler;
}

export class FakeApi {
  readonly requests: RecordedRequest[] = [];
  readonly #routes: Route[] = [];

  /** Answer `method path` with `handler`; a later registration of the same route wins. */
  on(method: string, path: string | RegExp, handler: Handler | Response): this {
    const answer = typeof handler === "function" ? handler : () => handler.clone();
    this.#routes.unshift({ method, path, handler: answer });
    return this;
  }

  /** The requests sent to `method path`. */
  sent(method: string, path: string): RecordedRequest[] {
    return this.requests.filter((request) => request.method === method && request.path === path);
  }

  send = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    const text = request.body === null ? "" : await request.clone().text();
    this.requests.push({
      method: request.method,
      path: url.pathname,
      headers: new Headers(request.headers),
      body: text === "" ? undefined : parse(text),
    });
    const route = this.#routes.find(
      (candidate) =>
        candidate.method === request.method &&
        (typeof candidate.path === "string"
          ? candidate.path === url.pathname
          : candidate.path.test(url.pathname)),
    );
    if (route === undefined) {
      return Response.json({ detail: "Not found" }, { status: 404 });
    }
    return route.handler(request, url);
  };
}

function parse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function tokenAnswer(accessToken = "access-1", expiresIn = 900): Response {
  return Response.json({ access_token: accessToken, token_type: "bearer", expires_in: expiresIn });
}

export const SELLER = {
  id: "0199a000-0000-7000-8000-000000000001",
  email: "demo-seller@livecart.test",
  slug: "demo-seller",
  display_name: "Demo Seller",
};

/** An API where the seller is signed in through the refresh cookie, with `token`. */
export function signedInApi(token = "access-1"): FakeApi {
  return new FakeApi()
    .on("POST", "/api/v1/auth/refresh", tokenAnswer(token))
    .on("GET", "/api/v1/auth/me", Response.json(SELLER));
}
