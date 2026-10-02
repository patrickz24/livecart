import { getOrder } from "@livecart/api-client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router";

import { ApiError, describeError, unwrap } from "../../api/errors";
import { keys } from "../../api/queries";
import { useServices } from "../../app/services";
import type { LiveOrder, LiveState } from "../../realtime/applyEvent";
import { ORDER_STATUS, SHIPMENT_STATUS } from "../../strings";
import { Alert, Badge, Loading, PageTitle } from "../../ui/feedback";
import { formatCountdown, formatDateTime, formatMoney } from "../../ui/format";
import { useNow } from "../../ui/useNow";

/** The order in a live view's cache, with its session: what the stream said of it. */
function fromLiveViews(
  lives: readonly (readonly [unknown, LiveState | undefined])[],
  reference: string,
): { order: LiveOrder; session: LiveState["session"] } | null {
  for (const [, state] of lives) {
    const order = state?.orders.find((candidate) => candidate.reference === reference);
    if (state !== undefined && order !== undefined) {
      return { order, session: state.session };
    }
  }
  return null;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-3 gap-4 px-4 py-3">
      <dt className="text-sm text-ink-muted">{label}</dt>
      <dd className="col-span-2 text-sm">{children}</dd>
    </div>
  );
}

/**
 * `/orders/:reference`. The API's projection of an order by its reference
 * (status, product, quantity, total, reservation), which shows no buyer
 * data; its payment date and shipment come from the live view of its
 * session, when the dashboard holds it. The address and the tracking page
 * need the order's own token: the buyer's, never the seller's (ADR 0007).
 */
export function OrderDetailPage() {
  const { reference = "" } = useParams();
  const { client } = useServices();
  const queryClient = useQueryClient();
  const order = useQuery({
    queryKey: keys.order(reference),
    queryFn: ({ signal }) => unwrap(getOrder({ client, path: { reference }, signal })),
  });
  const pending = order.data?.status === "pending_payment";
  const now = useNow(1000, pending);
  const live = fromLiveViews(
    queryClient.getQueriesData<LiveState>({ queryKey: ["live"] }),
    reference,
  );

  if (order.isError) {
    return (
      <section>
        <PageTitle title={`Order ${reference}`}>Order {reference}</PageTitle>
        <Alert className="mt-4">
          {order.error instanceof ApiError && order.error.status === 404
            ? "No order has this reference."
            : describeError(order.error)}
        </Alert>
      </section>
    );
  }
  if (order.data === undefined) {
    return <Loading />;
  }

  const { data } = order;
  const status = ORDER_STATUS[data.status];
  const shipment = live?.order.shipment ?? null;
  const paidAt = live?.order.paid_at ?? null;
  const left = new Date(data.reserved_until).getTime() - now;
  return (
    <section>
      <div className="flex flex-wrap items-center gap-3">
        <PageTitle title={`Order ${data.reference}`}>
          Order <span className="font-mono">{data.reference}</span>
        </PageTitle>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      {data.status === "paid_after_expiry" ? (
        <Alert className="mt-4">
          This order was paid after its reservation expired and its stock was released: refund the
          buyer by hand (docs/runbooks/paid-after-expiry.md).
        </Alert>
      ) : null}
      <dl className="mt-6 divide-y divide-line rounded-control border border-line bg-surface">
        <Row label="Product">
          {data.product.code}
          {data.product.variant === "" ? "" : ` · ${data.product.variant}`} — {data.product.name}
        </Row>
        <Row label="Quantity">{data.quantity}</Row>
        <Row label="Total">{formatMoney(data.total_minor, data.currency)}</Row>
        <Row label="Reserved until">
          {formatDateTime(data.reserved_until)}
          {pending ? (
            <span className="ml-2 font-mono text-ink-muted">
              ({left > 0 ? `${formatCountdown(left)} left` : "expiring"})
            </span>
          ) : null}
        </Row>
        {paidAt === null ? null : <Row label="Paid at">{formatDateTime(paidAt)}</Row>}
        <Row label="Shipment">
          {shipment === null ? (
            <span className="text-ink-muted">
              {live === null
                ? "Open the live view of its session to follow its shipment."
                : "No shipment yet: it is created once the order is paid."}
            </span>
          ) : (
            <span className="flex flex-wrap items-center gap-2">
              <Badge tone={SHIPMENT_STATUS[shipment.status].tone}>
                {SHIPMENT_STATUS[shipment.status].label}
              </Badge>
              <span className="font-mono">{shipment.tracking_number}</span>
            </span>
          )}
        </Row>
        {live === null ? null : (
          <Row label="Session">
            <Link to={`/sessions/${live.session.id}/live`} className="text-brand underline">
              {live.session.title}
            </Link>
          </Row>
        )}
      </dl>
    </section>
  );
}
