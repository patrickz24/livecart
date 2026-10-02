// The k6 2.3 APIs that the load test scripts call, typed by hand. k6 runs
// TypeScript by stripping the types, so these declarations only serve tsc and
// ESLint. The repository has no @types/k6 dependency, whose versions follow
// k6's own: a dozen signatures do not justify one. Keep this file to what the
// scripts use, as k6's documentation describes it.

/** The variables of the container (compose `environment`, `run -e`). */
declare const __ENV: Readonly<Record<string, string | undefined>>;

declare const console: {
  log(...data: unknown[]): void;
  error(...data: unknown[]): void;
};

declare module "k6" {
  export function check<T>(value: T, checkers: Record<string, (value: T) => boolean>): boolean;
  export function sleep(seconds: number): void;
}

declare module "k6/crypto" {
  export function hmac(algorithm: "sha256", secret: string, data: string, encoding: "hex"): string;
}

declare module "k6/execution" {
  const execution: {
    readonly scenario: {
      /** The zero-based number of the iteration within the scenario, across all VUs. */
      readonly iterationInTest: number;
      /** When the scenario started, in Unix milliseconds. */
      readonly startTime: number;
    };
  };
  export default execution;
}

declare module "k6/http" {
  /** What `expectedStatuses` returns: the statuses that do not count as failed. */
  export interface ResponseCallback {
    readonly kind: "expected statuses";
  }

  export interface Params {
    headers?: Record<string, string>;
    tags?: Record<string, string>;
    timeout?: string;
    responseCallback?: ResponseCallback;
  }

  export interface Response {
    /** 0 when no answer came, such as on a timeout. */
    readonly status: number;
    readonly body: string | null;
    readonly error: string;
    readonly timings: { readonly duration: number };
    json(): unknown;
  }

  const http: {
    get(url: string, params?: Params): Response;
    post(url: string, body: string, params?: Params): Response;
    expectedStatuses(...statuses: number[]): ResponseCallback;
  };
  export default http;
}

declare module "k6/metrics" {
  class Metric {
    constructor(name: string);
    add(value: number): void;
  }
  export class Counter extends Metric {}
  export class Gauge extends Metric {}
  export class Trend extends Metric {}
}
