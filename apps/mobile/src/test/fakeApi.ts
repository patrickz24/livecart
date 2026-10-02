/** A request the app sent to the fake API, as the server would read it. */
export interface RecordedRequest {
  method: string;
  /** The path and query, without the origin: `/api/v1/orders/LC-7K3F9Q`. */
  path: string;
  headers: Headers;
  body: unknown;
}

type Handler = (request: RecordedRequest) => Response | Promise<Response>;

export const FAKE_API_ORIGIN = "http://api.test";

/** A JSON answer, as FastAPI writes them. */
export function json(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/**
 * The API, in memory: the `fetch` given to the generated client answers each
 * `METHOD /path` with its handler, records every request, and answers 501 to
 * one it has no handler for, so that a test notices it.
 */
export class FakeApi {
  readonly requests: RecordedRequest[] = [];
  private readonly handlers = new Map<string, Handler>();

  /** Answers `method path` (the path without its query) with `handler`, or with that JSON. */
  on(method: string, path: string, handler: Handler | Response): this {
    this.handlers.set(`${method} ${path}`, () =>
      handler instanceof Response ? handler.clone() : handler(this.lastRequest()),
    );
    return this;
  }

  /** The requests sent to `method path`. */
  sent(method: string, path: string): RecordedRequest[] {
    return this.requests.filter(
      (request) => request.method === method && request.path.split("?")[0] === path,
    );
  }

  readonly fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    const text = await request.text();
    this.requests.push({
      method: request.method,
      path: `${url.pathname}${url.search}`,
      headers: request.headers,
      body: text === "" ? undefined : (JSON.parse(text) as unknown),
    });
    const handler = this.handlers.get(`${request.method} ${url.pathname}`);
    if (handler === undefined) {
      return json(501, {
        detail: `the fake API has no answer for ${request.method} ${url.pathname}`,
      });
    }
    return handler(this.lastRequest());
  };

  private lastRequest(): RecordedRequest {
    const last = this.requests.at(-1);
    if (last === undefined) {
      throw new Error("no request recorded yet");
    }
    return last;
  }
}
