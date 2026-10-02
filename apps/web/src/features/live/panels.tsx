import type { ProductResponse, RejectionReason } from "@livecart/api-client";
import type { ReactNode } from "react";
import { Link } from "react-router";

import type { LiveOrder, LiveState } from "../../realtime/applyEvent";
import { INTENT_OUTCOME, ORDER_STATUS, REJECTION_REASON, SHIPMENT_STATUS } from "../../strings";
import { Badge } from "../../ui/feedback";
import { formatCountdown, formatMoney } from "../../ui/format";
import { useNow } from "../../ui/useNow";

function Panel({
  id,
  title,
  summary,
  children,
}: {
  id: string;
  title: string;
  summary?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-control border border-line bg-surface">
      <header className="flex items-baseline justify-between gap-3 border-b border-line px-4 py-3">
        <h2 id={id} className="text-base font-semibold">
          {title}
        </h2>
        {summary === undefined ? null : <p className="text-xs text-ink-muted">{summary}</p>}
      </header>
      {/* A long list scrolls inside its panel: a named region, focusable so that
          the keyboard scrolls it too. */}
      <div role="region" aria-labelledby={id} tabIndex={0} className="max-h-[28rem] overflow-auto">
        {children}
      </div>
    </section>
  );
}

// Headers stay in view while a panel scrolls; cells keep references and amounts on one line.
const TH = "sticky top-0 bg-surface px-3 py-2 text-xs font-medium text-ink-muted uppercase";
const TD = "px-3 py-2 whitespace-nowrap";

function productLabel(product: ProductResponse | undefined): string {
  if (product === undefined) {
    return "Unknown product";
  }
  return product.variant === "" ? product.code : `${product.code} · ${product.variant}`;
}

/** Each product's stock: what is still for sale, reserved and sold. */
export function StockPanel({ products }: { products: LiveState["products"] }) {
  return (
    <Panel id="stock-heading" title="Stock" summary={`${String(products.length)} products`}>
      {products.length === 0 ? (
        <p className="px-4 py-3 text-sm text-ink-muted">This session has no products.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              <th scope="col" className={TH}>
                Product
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Available
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Reserved
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Sold
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Total
              </th>
            </tr>
          </thead>
          <tbody>
            {products.map((product) => (
              <tr key={product.id} className="border-t border-line">
                <th scope="row" className={`${TD} font-medium`}>
                  {productLabel(product)}
                  <span className="block text-xs font-normal text-ink-muted">
                    {product.name} · {formatMoney(product.price_minor, product.currency)}
                  </span>
                </th>
                <td className={`${TD} text-right`}>
                  <span className="font-semibold">{product.available}</span>
                  {product.available === 0 ? (
                    <span className="ml-2">
                      <Badge tone="danger">Sold out</Badge>
                    </span>
                  ) : null}
                  <meter
                    className="mt-1 block h-1.5 w-full"
                    min={0}
                    max={Math.max(product.stock_total, 1)}
                    value={product.available}
                    aria-label={`${productLabel(product)}: ${String(product.available)} of ${String(product.stock_total)} available`}
                  />
                </td>
                <td className={`${TD} text-right`}>{product.reserved}</td>
                <td className={`${TD} text-right`}>{product.sold}</td>
                <td className={`${TD} text-right`}>{product.stock_total}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Reservation({ order, now }: { order: LiveOrder; now: number }) {
  if (order.status !== "pending_payment") {
    return <span className="text-ink-muted">—</span>;
  }
  const left = new Date(order.reserved_until).getTime() - now;
  return (
    <time dateTime={order.reserved_until} className="font-mono tabular-nums">
      {left > 0 ? `${formatCountdown(left)} left` : "Expiring…"}
    </time>
  );
}

/** The orders, newest first: status, reservation countdown, shipment. */
export function OrdersPanel({ state }: { state: LiveState }) {
  const pending = state.orders.some((order) => order.status === "pending_payment");
  const now = useNow(1000, pending);
  const products = new Map(state.products.map((product) => [product.id, product]));
  return (
    <Panel
      id="orders-heading"
      title="Orders"
      summary={`Latest ${String(state.orders.length)}, newest first`}
    >
      {state.orders.length === 0 ? (
        <p className="px-4 py-3 text-sm text-ink-muted">No orders yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              <th scope="col" className={TH}>
                Reference
              </th>
              <th scope="col" className={TH}>
                Product
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Total
              </th>
              <th scope="col" className={TH}>
                Status
              </th>
              <th scope="col" className={TH}>
                Reservation
              </th>
              <th scope="col" className={TH}>
                Shipment
              </th>
            </tr>
          </thead>
          <tbody>
            {state.orders.map((order) => {
              const status = ORDER_STATUS[order.status];
              return (
                <tr key={order.order_id} className="border-t border-line">
                  <th scope="row" className={`${TD} font-mono font-medium whitespace-nowrap`}>
                    <Link to={`/orders/${order.reference}`} className="text-brand underline">
                      {order.reference}
                    </Link>
                  </th>
                  <td className={TD}>
                    {productLabel(products.get(order.product_id))} × {order.quantity}
                  </td>
                  <td className={`${TD} text-right`}>
                    {formatMoney(order.total_minor, order.currency)}
                  </td>
                  <td className={TD}>
                    <Badge
                      tone={status.tone}
                      title={
                        order.status === "paid_after_expiry"
                          ? "Paid after its reservation expired: refund it by hand"
                          : undefined
                      }
                    >
                      {status.label}
                    </Badge>
                  </td>
                  <td className={TD}>
                    <Reservation order={order} now={now} />
                  </td>
                  <td className={TD}>
                    {order.shipment === null ? (
                      <span className="text-ink-muted">—</span>
                    ) : (
                      <>
                        <Badge tone={SHIPMENT_STATUS[order.shipment.status].tone}>
                          {SHIPMENT_STATUS[order.shipment.status].label}
                        </Badge>
                        <span className="block font-mono text-xs text-ink-muted">
                          {order.shipment.tracking_number}
                        </span>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/** The purchase intents, newest first; the LLM's are marked "AI". */
export function IntentsPanel({ state }: { state: LiveState }) {
  const products = new Map(state.products.map((product) => [product.id, product]));
  const references = new Map(state.orders.map((order) => [order.order_id, order.reference]));
  return (
    <Panel
      id="intents-heading"
      title="Purchase intents"
      summary={`Latest ${String(state.intents.length)}, newest first`}
    >
      {state.intents.length === 0 ? (
        <p className="px-4 py-3 text-sm text-ink-muted">No purchase intent yet.</p>
      ) : (
        <ul className="divide-y divide-line">
          {state.intents.map((intent) => {
            const outcome = INTENT_OUTCOME[intent.outcome];
            const reference =
              intent.order_id === null ? undefined : references.get(intent.order_id);
            return (
              <li key={intent.intent_id} className="flex flex-wrap items-center gap-2 px-4 py-2">
                <span className="font-medium">
                  {productLabel(products.get(intent.product_id))} × {intent.quantity}
                </span>
                {intent.source === "llm" ? (
                  <Badge
                    tone="brand"
                    title={`Read by the LLM fallback, confidence ${intent.confidence}`}
                  >
                    AI
                    <span className="sr-only">: read by the LLM fallback</span>
                  </Badge>
                ) : null}
                <Badge tone={outcome.tone}>{outcome.label}</Badge>
                {reference === undefined ? null : (
                  <Link
                    to={`/orders/${reference}`}
                    className="ml-auto font-mono text-xs text-brand underline"
                  >
                    {reference}
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/** The comments that carried no intent, counted by reason. */
export function RejectionsPanel({ rejections }: { rejections: LiveState["rejections"] }) {
  const counts = new Map<RejectionReason, number>();
  for (const rejection of rejections) {
    counts.set(rejection.reason, (counts.get(rejection.reason) ?? 0) + 1);
  }
  const rows = [...counts].toSorted(([, a], [, b]) => b - a);
  return (
    <Panel
      id="rejections-heading"
      title="Rejected comments"
      summary={`Latest ${String(rejections.length)}, by reason`}
    >
      {rows.length === 0 ? (
        <p className="px-4 py-3 text-sm text-ink-muted">No rejected comment yet.</p>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map(([reason, count]) => (
            <li key={reason} className="flex items-center justify-between px-4 py-2 text-sm">
              <span>{REJECTION_REASON[reason]}</span>
              <span className="font-semibold tabular-nums">{count}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
