/** An answer of the API other than 2xx, with its parsed body. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  /** The `Retry-After` of a 429 or a 503, in seconds, when the API sent one. */
  readonly retryAfterSeconds: number | null;

  constructor(status: number, body: unknown, retryAfterSeconds: number | null = null) {
    super(`The API answered ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** The `detail` message of the API's `ErrorResponse`, when it has one. */
  get detail(): string | null {
    const detail = isRecord(this.body) ? this.body.detail : undefined;
    return typeof detail === "string" ? detail : null;
  }
}

/** The request never got an answer: no network, or the API out of reach. */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("The API could not be reached", { cause });
    this.name = "NetworkError";
  }
}

/** What a generated SDK function resolves to: its data or its error, and the response. */
interface SdkAnswer {
  data?: unknown;
  error?: unknown;
  response?: Response;
}

/**
 * The data of a generated SDK call, or its failure thrown: `ApiError` for an
 * answer other than 2xx, `NetworkError` for none. The SDK functions return
 * their error rather than throw it, so every query goes through here.
 */
export async function unwrap<A extends SdkAnswer>(
  answer: Promise<A>,
): Promise<Exclude<A["data"], undefined>> {
  const result = await answer;
  const { response } = result;
  if (response === undefined) {
    throw new NetworkError(result.error);
  }
  if (!response.ok || result.error !== undefined) {
    throw new ApiError(response.status, result.error, retryAfter(response));
  }
  // A 2xx without an error always carries its data.
  return result.data as Exclude<A["data"], undefined>;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** Whether a failed query is worth retrying: never for a 4xx, the request itself is refused. */
export function isRetryable(error: unknown): boolean {
  return !(error instanceof ApiError && error.status >= 400 && error.status < 500);
}

/** A sentence for the buyer, for any failure of a request. */
export function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "The server cannot be reached. Check your connection and try again.";
  }
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return error.retryAfterSeconds === null
        ? "Too many requests. Wait a moment, then try again."
        : `Too many requests. Try again in ${String(error.retryAfterSeconds)} s.`;
    }
    if (error.status >= 500) {
      return "The server is unavailable right now. Try again in a moment.";
    }
    return error.detail ?? `The request was refused (${String(error.status)}).`;
  }
  return "Something went wrong. Try again.";
}

function retryAfter(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  const seconds = header === null ? Number.NaN : Number.parseInt(header, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
