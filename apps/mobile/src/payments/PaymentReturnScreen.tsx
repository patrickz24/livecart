import { getOrderDetails } from "@livecart/api-client";
import { useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useCallback, useEffect } from "react";
import { ActivityIndicator, Text, View } from "react-native";

import { useApiClient } from "../api/ApiProvider";
import { unwrap } from "../api/errors";
import { useBrandTheme } from "../brand/brandTheme";
import { useOrderToken } from "../orders/OrderAccessProvider";
import { orderKeys } from "../orders/orderQueries";
import { parseOrderReference } from "../orders/reference";
import { Notice } from "../ui/Notice";

import { confirmationDelays } from "./paymentReturn";
import { usePaymentConfirmation, type Confirmation } from "./usePaymentConfirmation";

// One schedule for the app: 3 s, then longer, for a minute at most.
const DELAYS = confirmationDelays();

/**
 * Where the bridge page sends the buyer back:
 * `<scheme>://payment-return?status=success|cancel&reference=LC-7K3F9Q`.
 * The link only says how the buyer left the provider's page, and carries no
 * token: whether the order is paid is the API's to say, after the provider's
 * webhook. On success, the screen reads the order's status with its token
 * every 3 s, then less often, for 60 s at most.
 */
export function PaymentReturnScreen() {
  const params = useLocalSearchParams<"/payment-return">();
  const reference = parseOrderReference(
    typeof params.reference === "string" ? params.reference : "",
  );
  const status = params.status === "success" || params.status === "cancel" ? params.status : null;
  const token = useOrderToken(reference);
  const { color, space } = useBrandTheme();
  const router = useRouter();
  const toOrder = () => {
    if (reference !== null) {
      router.dismissTo({ pathname: "/orders/[reference]", params: { reference } });
    }
  };

  let content;
  if (reference === null || status === null) {
    content = <Notice tone="error" title="This link is not a payment return" />;
  } else if (token.isPending) {
    content = <ActivityIndicator color={color.primary} />;
  } else if (token.data == null) {
    content = <Notice title="This order is not on this phone" />;
  } else if (status === "cancel") {
    content = (
      <Notice
        title="Payment cancelled"
        message={`Order ${reference} is not paid. You can pay for it while it is reserved.`}
        action={{ label: "Back to the order", onPress: toOrder }}
      />
    );
  } else {
    content = <PaymentConfirmation reference={reference} token={token.data} onDone={toOrder} />;
  }
  return (
    <View style={{ flex: 1, padding: space.lg, backgroundColor: color.background }}>{content}</View>
  );
}

function PaymentConfirmation({
  reference,
  token,
  onDone,
}: {
  reference: string;
  token: string;
  onDone: () => void;
}) {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { color, space, fontSize } = useBrandTheme();
  // The token route, limited per token: a live audience behind one carrier's
  // address does not share one budget, as it would on the public lookup.
  const readStatus = useCallback(
    async () =>
      (await unwrap(getOrderDetails({ client, path: { reference }, auth: token }))).status,
    [client, reference, token],
  );
  const confirmation = usePaymentConfirmation(readStatus, DELAYS);

  useEffect(() => {
    if (confirmation.kind === "settled") {
      void queryClient.invalidateQueries({ queryKey: orderKeys.all(reference) });
    }
  }, [confirmation.kind, queryClient, reference]);

  if (confirmation.kind === "confirming") {
    return (
      <View style={{ gap: space.md, alignItems: "center" }}>
        <ActivityIndicator color={color.primary} />
        <Text style={{ color: color.text, fontSize: fontSize.body }}>
          Confirming the payment of {reference}…
        </Text>
      </View>
    );
  }
  return (
    <Notice
      {...settledNotice(confirmation, reference)}
      action={{ label: "See the order", onPress: onDone }}
    />
  );
}

function settledNotice(
  confirmation: Exclude<Confirmation, { kind: "confirming" }>,
  reference: string,
): { title: string; message: string; tone?: "error" } {
  switch (confirmation.kind) {
    case "settled":
      if (confirmation.status === "paid") {
        return {
          title: "Payment confirmed",
          message: `Order ${reference} is paid: it ships soon.`,
        };
      }
      if (confirmation.status === "paid_after_expiry") {
        return {
          title: "Paid after the reservation ended",
          message:
            "The payment arrived too late for the reserved stock: the seller will contact you.",
        };
      }
      return {
        tone: "error",
        title: "The reservation has ended",
        message: `Order ${reference} expired before its payment was confirmed.`,
      };
    case "timed_out":
      return {
        title: "Still being confirmed",
        message:
          "The payment has not been confirmed yet. The order shows it as soon as the payment provider confirms it.",
      };
    case "not_found":
      return {
        tone: "error",
        title: "This order cannot be found",
        message: "The server knows no order with this reference and this token.",
      };
  }
}
