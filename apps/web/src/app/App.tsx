import { getHealth, type Client } from "@livecart/api-client";
import { useEffect, useState } from "react";

type ApiStatus = "checking" | "up" | "unreachable";

const STATUS_TEXT: Record<ApiStatus, string> = {
  checking: "Checking the API…",
  up: "API is up",
  unreachable: "API is unreachable",
};

/** Milestone 1 shell: proves the page reaches the API through the generated client. */
export function App({ client }: { client: Client }) {
  const [status, setStatus] = useState<ApiStatus>("checking");

  useEffect(() => {
    const controller = new AbortController();
    // Without throwOnError, network and HTTP errors resolve with `error` set.
    void getHealth({ client, signal: controller.signal }).then(({ data }) => {
      if (!controller.signal.aborted) {
        setStatus(data?.status === "ok" ? "up" : "unreachable");
      }
    });
    return () => {
      controller.abort();
    };
  }, [client]);

  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="text-2xl font-semibold">LiveCart</h1>
      <p role="status" className="mt-2 text-ink-muted">
        {STATUS_TEXT[status]}
      </p>
    </main>
  );
}
