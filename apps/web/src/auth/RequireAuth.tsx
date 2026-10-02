import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { Navigate, useLocation, type Location } from "react-router";

import { useServices } from "../app/services";
import { Button } from "../ui/Button";
import { Alert, Loading } from "../ui/feedback";
import type { AuthStatus } from "./session";

/** Where the login page sends the seller back, carried in the navigation state. */
export interface LoginState {
  from?: Pick<Location, "pathname" | "search" | "hash">;
}

export function useAuthStatus(): AuthStatus {
  const { session } = useServices();
  return useSyncExternalStore(session.subscribe, session.getStatus);
}

/**
 * The pages of a signed-in seller. A tab starts without an access token (it
 * lives in memory only), so the first visit trades the refresh cookie for
 * one; no cookie, or a refused one, leads to the login page, which brings the
 * seller back here. A session that ends later (a refresh refused, a logout)
 * leads there too. A server that cannot be reached is no verdict: the page
 * offers to try again rather than signing the seller out.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session } = useServices();
  const status = useAuthStatus();
  const location = useLocation();
  const [unreachable, setUnreachable] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (status !== "unknown") {
      return;
    }
    let current = true;
    session.refresh().catch(() => {
      if (current) {
        setUnreachable(true);
      }
    });
    return () => {
      current = false;
    };
  }, [session, status, attempt]);

  if (status === "signed_in") {
    return children;
  }
  if (status === "signed_out") {
    // After a logout the next seller starts from the list, not from this page.
    const state: LoginState =
      session.endReason === "expired" || session.endReason === null ? { from: location } : {};
    return <Navigate to="/login" replace state={state} />;
  }
  if (unreachable) {
    return (
      <main className="mx-auto max-w-md p-8">
        <Alert>The server cannot be reached, so you cannot be signed in yet.</Alert>
        <Button
          className="mt-4"
          onClick={() => {
            setUnreachable(false);
            setAttempt((count) => count + 1);
          }}
        >
          Try again
        </Button>
      </main>
    );
  }
  return (
    <main className="mx-auto max-w-md p-8">
      <Loading>Signing you in…</Loading>
    </main>
  );
}
