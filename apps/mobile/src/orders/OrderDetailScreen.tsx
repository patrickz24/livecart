import { fontWeight } from "@livecart/design-tokens";
import type { OrderDetailsResponse, TrackingResponse } from "@livecart/api-client";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import type { ComponentProps, ReactNode } from "react";
import { ActivityIndicator, RefreshControl, ScrollView, Text, View } from "react-native";

import { useApiClient } from "../api/ApiProvider";
import { describeError, isNotFound } from "../api/errors";
import { useBrandTheme } from "../brand/brandTheme";
import { Notice } from "../ui/Notice";
import { formatDateTime, formatMoney } from "../ui/format";

import { storedOrdersKey, useOrderAccess } from "./OrderAccessProvider";
import {
  ORDER_STATUS_TEXT,
  SHIPMENT_STATUS_TEXT,
  SHIPMENT_STEPS,
  isPaid,
  orderDetailsQuery,
  orderKeys,
  orderTrackingQuery,
} from "./orderQueries";
import { parseOrderReference } from "./reference";

/**
 * One order of this phone: its status, product, total and address, then,
 * once paid, where its parcel is. Every request carries the order's token
 * from SecureStore in its Authorization header.
 */
export function OrderDetailScreen() {
  const params = useLocalSearchParams<"/orders/[reference]">();
  const reference = parseOrderReference(params.reference);
  const store = useOrderAccess();
  const { color, space } = useBrandTheme();
  const token = useQuery({
    queryKey: ["order-token", reference],
    queryFn: () => (reference === null ? null : store.tokenFor(reference)),
  });

  const title = <Stack.Screen options={{ title: reference ?? "Order" }} />;
  if (token.isPending) {
    return <ActivityIndicator style={{ margin: space.xl }} color={color.primary} />;
  }
  if (reference === null || token.data == null) {
    return (
      <Screen>
        {title}
        <Notice
          title="This order is not on this phone"
          message="Add it with the reference and the token you received, or open its link."
        />
      </Screen>
    );
  }
  return (
    <>
      {title}
      <OrderDetails reference={reference} token={token.data} />
    </>
  );
}

function OrderDetails({ reference, token }: { reference: string; token: string }) {
  const client = useApiClient();
  const store = useOrderAccess();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { color, space } = useBrandTheme();
  const details = useQuery(orderDetailsQuery(client, reference, token));
  const paid = details.data !== undefined && isPaid(details.data.status);
  const tracking = useQuery({ ...orderTrackingQuery(client, reference, token), enabled: paid });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: orderKeys.all(reference) });
  const forget = async () => {
    await store.remove(reference);
    await queryClient.invalidateQueries({ queryKey: storedOrdersKey });
    router.dismissTo("/orders");
  };

  if (details.isPending) {
    return <ActivityIndicator style={{ margin: space.xl }} color={color.primary} />;
  }
  return (
    <Screen
      refreshControl={
        <RefreshControl
          refreshing={details.isRefetching || tracking.isRefetching}
          onRefresh={refresh}
          colors={[color.primary]}
        />
      }
    >
      {details.isError ? (
        isNotFound(details.error) ? (
          // A wrong token answers the 404 of an unknown reference: the API tells nothing more.
          <Notice
            tone="error"
            title="This order cannot be found"
            message="The server knows no order with this reference and this token. It may have been reset."
            action={{ label: "Remove it from this phone", onPress: () => void forget() }}
          />
        ) : (
          <Notice
            tone="error"
            title="The order cannot be loaded"
            message={describeError(details.error)}
            action={{ label: "Try again", onPress: refresh }}
          />
        )
      ) : (
        <>
          <OrderSummary order={details.data} />
          {paid ? <Tracking tracking={tracking} /> : null}
        </>
      )}
    </Screen>
  );
}

function Screen({
  children,
  refreshControl,
}: {
  children: ReactNode;
  refreshControl?: ComponentProps<typeof ScrollView>["refreshControl"];
}) {
  const { color, space } = useBrandTheme();
  return (
    <ScrollView
      style={{ backgroundColor: color.background }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={refreshControl}
    >
      {children}
    </ScrollView>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  const { color, space, radius, fontSize } = useBrandTheme();
  return (
    <View
      style={{
        gap: space.sm,
        padding: space.lg,
        borderRadius: radius.md,
        borderWidth: 1,
        borderColor: color.border,
        backgroundColor: color.surface,
      }}
    >
      <Text
        accessibilityRole="header"
        style={{ color: color.textMuted, fontSize: fontSize.caption, fontWeight: fontWeight.bold }}
      >
        {title.toUpperCase()}
      </Text>
      {children}
    </View>
  );
}

function Line({ children, strong }: { children: ReactNode; strong?: boolean }) {
  const { color, fontSize } = useBrandTheme();
  return (
    <Text
      style={{
        color: color.text,
        fontSize: strong === true ? fontSize.title : fontSize.body,
        fontWeight: strong === true ? fontWeight.bold : fontWeight.regular,
      }}
    >
      {children}
    </Text>
  );
}

function OrderSummary({ order }: { order: OrderDetailsResponse }) {
  const product =
    order.product.variant === ""
      ? `${order.product.code} · ${order.product.name}`
      : `${order.product.code} ${order.product.variant} · ${order.product.name}`;
  const address = order.shipping_address;
  return (
    <>
      <Card title="Status">
        <Line strong>{ORDER_STATUS_TEXT[order.status]}</Line>
        {order.status === "pending_payment" ? (
          <Line>Reserved until {formatDateTime(order.reserved_until)}</Line>
        ) : null}
        {order.paid_at === null ? null : <Line>Paid on {formatDateTime(order.paid_at)}</Line>}
      </Card>
      <Card title="Order">
        <Line>{product}</Line>
        <Line>
          {String(order.quantity)} × {formatMoney(order.unit_price_minor, order.currency)}
        </Line>
        <Line strong>{formatMoney(order.total_minor, order.currency)}</Line>
      </Card>
      {address === null ? null : (
        <Card title="Delivery address">
          <Line>{address.full_name}</Line>
          <Line>{address.line1}</Line>
          {address.line2 === undefined || address.line2 === "" ? null : (
            <Line>{address.line2}</Line>
          )}
          <Line>
            {address.postal_code} {address.city}, {address.country}
          </Line>
        </Card>
      )}
    </>
  );
}

function Tracking({ tracking }: { tracking: UseQueryResult<TrackingResponse> }) {
  const { color, space, fontSize } = useBrandTheme();
  if (tracking.isPending) {
    return <ActivityIndicator color={color.primary} />;
  }
  if (tracking.isError) {
    return isNotFound(tracking.error) ? (
      // The token holds: the worker has not created the shipment yet.
      <Card title="Delivery">
        <Line>Not shipped yet: the parcel is being prepared.</Line>
      </Card>
    ) : (
      <Card title="Delivery">
        <Line>{describeError(tracking.error)}</Line>
      </Card>
    );
  }
  const parcel = tracking.data;
  const reached = SHIPMENT_STEPS.indexOf(parcel.status);
  return (
    <Card title="Delivery">
      <Line strong>{SHIPMENT_STATUS_TEXT[parcel.status]}</Line>
      <Line>
        {parcel.carrier.toUpperCase()} · {parcel.tracking_number}
      </Line>
      <Line>Last update {formatDateTime(parcel.last_event_at)}</Line>
      {parcel.status === "cancelled" ? null : (
        <View style={{ gap: space.xs, marginTop: space.sm }}>
          {SHIPMENT_STEPS.map((step, index) => (
            <Text
              key={step}
              style={{
                color: index <= reached ? color.success : color.textMuted,
                fontSize: fontSize.body,
                fontWeight: index === reached ? fontWeight.bold : fontWeight.regular,
              }}
            >
              {index <= reached ? "●" : "○"} {SHIPMENT_STATUS_TEXT[step]}
            </Text>
          ))}
        </View>
      )}
    </Card>
  );
}
