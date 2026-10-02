import type { Client } from "@livecart/api-client";
import { createContext, useContext, type ReactNode } from "react";

import { createApiClient } from "../api/client";
import { requestRefresh } from "../auth/refreshRequest";
import { AuthSession, browserLocks, type RefreshLocks } from "../auth/session";

/** What the pages use to reach the API: one of each per tab, injected in tests. */
export interface Services {
  client: Client;
  session: AuthSession;
  /** The plain `fetch`, for the event stream, which handles its own 401. */
  send: (request: Request) => Promise<Response>;
}

export function createServices(
  options: { send?: (request: Request) => Promise<Response>; locks?: RefreshLocks | null } = {},
): Services {
  const send = options.send ?? ((request: Request) => globalThis.fetch(request));
  // The session refreshes through the client, and the client asks the session
  // for its token: the closure reads the client, built right after, when called.
  const session = new AuthSession({
    locks: options.locks === undefined ? browserLocks() : options.locks,
    requestRefresh: () => requestRefresh(client),
  });
  const client = createApiClient({ session, send });
  return { client, session, send };
}

const ServicesContext = createContext<Services | null>(null);

export function ServicesProvider({
  services,
  children,
}: {
  services: Services;
  children: ReactNode;
}) {
  return <ServicesContext value={services}>{children}</ServicesContext>;
}

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (services === null) {
    throw new Error("useServices must be used inside a ServicesProvider");
  }
  return services;
}
