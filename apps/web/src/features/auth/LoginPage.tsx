import { login } from "@livecart/api-client";
import { useState, type SubmitEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { ApiError, describeError, unwrap } from "../../api/errors";
import { useServices } from "../../app/services";
import { grantOf } from "../../auth/refreshRequest";
import { useAuthStatus, type LoginState } from "../../auth/RequireAuth";
import type { SignOutReason } from "../../auth/session";
import { Button } from "../../ui/Button";
import { TextField } from "../../ui/fields";
import { Alert, PageTitle } from "../../ui/feedback";

const ENDED: Record<SignOutReason, { tone: "info" | "warning"; text: string }> = {
  logged_out: { tone: "info", text: "You are signed out." },
  logout_unconfirmed: {
    tone: "warning",
    text: "You are signed out of this tab, but the server could not be reached to end the session.",
  },
  expired: { tone: "info", text: "Your session has ended. Sign in again to continue." },
};

function loginError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "Invalid e-mail or password.";
  }
  if (error instanceof ApiError && error.status === 503) {
    return "Sign-in is unavailable right now. Try again in a moment.";
  }
  return describeError(error);
}

function textOf(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function destination(state: unknown): string {
  const from = (state as LoginState | null)?.from;
  if (from === undefined || from.pathname === "/login") {
    return "/sessions";
  }
  return `${from.pathname}${from.search}${from.hash}`;
}

/** `/login`: the e-mail and password of a seller account; no public sign-up. */
export function LoginPage() {
  const { client, session } = useServices();
  const status = useAuthStatus();
  const location = useLocation();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  if (status === "signed_in" && !pending) {
    return <Navigate to={destination(location.state)} replace />;
  }

  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError(null);
    try {
      const token = await unwrap(
        login({
          client,
          body: { email: textOf(form, "email"), password: textOf(form, "password") },
        }),
      );
      session.signIn(grantOf(token));
      await navigate(destination(location.state), { replace: true });
    } catch (failure) {
      setError(loginError(failure));
      setPending(false);
    }
  }

  const ended = session.endReason === null ? null : ENDED[session.endReason];

  return (
    <main className="mx-auto mt-16 max-w-sm px-4">
      <p className="mb-6 text-lg font-semibold text-brand">LiveCart</p>
      <PageTitle title="Sign in">Sign in to your dashboard</PageTitle>
      {ended === null || error !== null ? null : (
        <Alert tone={ended.tone} className="mt-4">
          {ended.text}
        </Alert>
      )}
      <form
        className="mt-6 space-y-4 rounded-control border border-line bg-surface p-6"
        onSubmit={(event) => void submit(event)}
        noValidate
      >
        {error === null ? null : <Alert>{error}</Alert>}
        <TextField
          label="E-mail"
          name="email"
          type="email"
          autoComplete="username"
          required
          maxLength={254}
        />
        <TextField
          label="Password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={128}
        />
        <Button type="submit" variant="primary" className="w-full" disabled={pending}>
          {pending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </main>
  );
}
