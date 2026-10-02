import { getCurrentSeller, logout } from "@livecart/api-client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router";

import { unwrap } from "../api/errors";
import { Button } from "../ui/Button";
import { useServices } from "./services";

/** The frame of every signed-in page: skip link, header, navigation, logout. */
export function Layout() {
  const { client, session } = useServices();
  const queryClient = useQueryClient();
  const [signingOut, setSigningOut] = useState(false);
  const me = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => unwrap(getCurrentSeller({ client, signal })),
    staleTime: Infinity,
  });

  async function signOut() {
    setSigningOut(true);
    let confirmed = true;
    try {
      // Revokes the cookie's whole family and clears the cookie (204).
      await unwrap(logout({ client }));
    } catch {
      confirmed = false;
    }
    // Nothing of this seller stays in the tab; RequireAuth leads to /login.
    queryClient.clear();
    session.signOut(confirmed ? "logged_out" : "logout_unconfirmed");
  }

  return (
    <div className="min-h-screen">
      <a
        href="#main"
        className="sr-only rounded-control bg-surface px-3 py-2 focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
      >
        Skip to main content
      </a>
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-3">
          <Link to="/sessions" className="text-lg font-semibold text-brand">
            LiveCart
          </Link>
          <nav aria-label="Main" className="flex gap-1">
            <NavLink
              to="/sessions"
              className={({ isActive }) =>
                `rounded-control px-3 py-2 text-sm font-medium ${isActive ? "bg-brand-soft text-brand-strong" : "text-ink-muted hover:text-ink"}`
              }
            >
              Sessions
            </NavLink>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            {me.data === undefined ? null : (
              <span className="text-sm text-ink-muted">
                Signed in as <span className="font-medium text-ink">{me.data.display_name}</span>
              </span>
            )}
            <Button onClick={() => void signOut()} disabled={signingOut}>
              {signingOut ? "Logging out…" : "Log out"}
            </Button>
          </div>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-6 focus:outline-none">
        <Outlet />
      </main>
    </div>
  );
}
