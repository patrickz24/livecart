import type { LiveSessionResponse } from "@livecart/api-client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";

import { ApiError, describeError } from "../../api/errors";
import { keys, productsQuery, sessionQuery } from "../../api/queries";
import { useServices } from "../../app/services";
import { SESSION_STATUS } from "../../strings";
import { Alert, Badge, Loading, PageTitle } from "../../ui/feedback";
import { LifecycleControls } from "./LifecycleControls";
import { SessionForm } from "./SessionForm";

interface SavedNotice {
  saved?: "created";
}

/** `/sessions/new`: a draft session and its first products. */
export function NewSessionPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  async function onSaved(session: LiveSessionResponse) {
    await queryClient.invalidateQueries({ queryKey: keys.sessions });
    const state: SavedNotice = { saved: "created" };
    await navigate(`/sessions/${session.id}/edit`, { replace: true, state });
  }

  return (
    <section>
      <PageTitle title="New live session">New live session</PageTitle>
      <p className="mt-1 text-sm text-ink-muted">
        A new session is a draft: comments reserve nothing until you start it.
      </p>
      <SessionForm session={null} products={[]} onSaved={onSaved} />
    </section>
  );
}

/** `/sessions/:sessionId/edit`: the session and its products, as the API holds them. */
export function EditSessionPage() {
  const { sessionId = "" } = useParams();
  const { client } = useServices();
  const queryClient = useQueryClient();
  const location = useLocation();
  const session = useQuery(sessionQuery(client, sessionId));
  const products = useQuery(productsQuery(client, sessionId));
  // A new form after each save, with the API's answer as its defaults.
  const [version, setVersion] = useState(0);
  const [notice, setNotice] = useState<string | null>(
    (location.state as SavedNotice | null)?.saved === "created" ? "Session created." : null,
  );

  async function onSaved() {
    await Promise.all([
      queryClient.refetchQueries({ queryKey: keys.session(sessionId), exact: true }),
      queryClient.refetchQueries({ queryKey: keys.products(sessionId), exact: true }),
      queryClient.invalidateQueries({ queryKey: keys.sessions }),
    ]);
    setVersion((current) => current + 1);
    setNotice("Changes saved.");
  }

  const failure = session.error ?? products.error;
  if (failure !== null) {
    return (
      <section>
        <PageTitle title="Live session">Live session</PageTitle>
        <Alert className="mt-4">
          {failure instanceof ApiError && failure.status === 404
            ? "This session does not exist, or it is not one of yours."
            : describeError(failure)}
        </Alert>
      </section>
    );
  }
  if (session.data === undefined || products.data === undefined) {
    return <Loading />;
  }

  const status = SESSION_STATUS[session.data.status];
  return (
    <section>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <PageTitle title={`Edit ${session.data.title}`}>{session.data.title}</PageTitle>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>
        <LifecycleControls session={session.data} />
      </div>
      {notice === null ? null : (
        <Alert tone="success" className="mt-4">
          {notice}
        </Alert>
      )}
      <SessionForm
        key={version}
        session={session.data}
        products={products.data}
        onSaved={onSaved}
      />
    </section>
  );
}
