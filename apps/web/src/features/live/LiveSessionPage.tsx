import { Link, useParams } from "react-router";

import type { StreamStatus } from "../../realtime/sseClient";
import { PLATFORM_LABELS, SESSION_STATUS } from "../../strings";
import { Alert, Badge, Loading, PageTitle } from "../../ui/feedback";
import { LifecycleControls } from "../sessions/LifecycleControls";
import { IntentsPanel, OrdersPanel, RejectionsPanel, StockPanel } from "./panels";
import { useLiveSession } from "./useLiveSession";

const CONNECTION: Record<StreamStatus, { label: string; dot: string }> = {
  connecting: { label: "Connecting…", dot: "bg-warning" },
  open: { label: "Live updates on", dot: "bg-success" },
  interrupted: { label: "Reconnecting…", dot: "bg-danger" },
  signed_out: { label: "Signed out", dot: "bg-ink-muted" },
  not_found: { label: "Not found", dot: "bg-ink-muted" },
  closed: { label: "Stopped", dot: "bg-ink-muted" },
};

/** `/sessions/:sessionId/live`: intents, rejections, orders and stock as they happen. */
export function LiveSessionPage() {
  const { sessionId = "" } = useParams();
  const { state, status, catchingUp } = useLiveSession(sessionId);

  if (status === "not_found") {
    return (
      <section>
        <PageTitle title="Live session">Live session</PageTitle>
        <Alert className="mt-4">This session does not exist, or it is not one of yours.</Alert>
      </section>
    );
  }

  const connection = CONNECTION[status];
  const indicator = (
    <p className="flex items-center gap-2 text-sm text-ink-muted">
      <span aria-hidden="true" className={`inline-block size-2 rounded-full ${connection.dot}`} />
      {connection.label}
    </p>
  );
  // The outage banner: the stream has been down for 5 s and is still retrying.
  const banner =
    status === "interrupted" ? (
      <Alert tone="warning" className="mt-4">
        Live updates are interrupted: the dashboard keeps trying to reconnect, and catches up on
        what it missed once it is back. What you see may be out of date.
      </Alert>
    ) : catchingUp ? (
      <Alert tone="info" className="mt-4">
        Some events were missed: reloading the session…
      </Alert>
    ) : null;

  if (state === undefined) {
    return (
      <section>
        <PageTitle title="Live session">Live session</PageTitle>
        {indicator}
        {banner}
        <Loading>Loading the session…</Loading>
      </section>
    );
  }

  const { session } = state;
  const sessionStatus = SESSION_STATUS[session.status];
  return (
    <section aria-busy={catchingUp}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <PageTitle title={`Live: ${session.title}`}>{session.title}</PageTitle>
            <Badge tone={sessionStatus.tone}>{sessionStatus.label}</Badge>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            {PLATFORM_LABELS[session.platform]} · {session.external_live_id} ·{" "}
            <Link to={`/sessions/${session.id}/edit`} className="font-medium text-brand underline">
              Edit the session
            </Link>
          </p>
          {indicator}
        </div>
        <LifecycleControls session={session} />
      </div>
      {banner}
      <div className={`mt-6 grid gap-6 xl:grid-cols-3 ${catchingUp ? "opacity-60" : ""}`.trim()}>
        <StockPanel products={state.products} />
        <div className="xl:col-span-2">
          <OrdersPanel state={state} />
        </div>
        <div className="xl:col-span-2">
          <IntentsPanel state={state} />
        </div>
        <RejectionsPanel rejections={state.rejections} />
      </div>
    </section>
  );
}
