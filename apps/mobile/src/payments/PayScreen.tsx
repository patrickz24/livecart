import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Platform, ScrollView, Text, View } from "react-native";

import { useApiClient } from "../api/ApiProvider";
import { describeError } from "../api/errors";
import { useBrandTheme } from "../brand/brandTheme";
import { useOrderToken } from "../orders/OrderAccessProvider";
import { orderDetailsQuery, orderKeys } from "../orders/orderQueries";
import { parseOrderReference } from "../orders/reference";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { TextField } from "../ui/TextField";
import { formatDateTime, formatMoney } from "../ui/format";

import { fieldsOf, validateAddress, type AddressFields, type AddressProblems } from "./address";
import { paymentReturnUrl } from "./paymentReturn";
import { openCheckoutSession, payInBrowser } from "./startCheckout";

/** Pay for an order: its delivery address, typed here, then the provider's hosted page. */
export function PayScreen() {
  const params = useLocalSearchParams<"/orders/[reference]/pay">();
  const reference = parseOrderReference(params.reference);
  const token = useOrderToken(reference);
  const { color, space } = useBrandTheme();

  if (token.isPending) {
    return (
      <ActivityIndicator
        style={{ margin: space.xl }}
        color={color.primary}
        accessibilityLabel="Loading the order"
      />
    );
  }
  if (reference === null || token.data == null) {
    return (
      <View style={{ padding: space.lg }}>
        <Notice title="This order is not on this phone" />
      </View>
    );
  }
  return <Checkout reference={reference} token={token.data} />;
}

const FIELDS: { name: keyof AddressFields; label: string; optional?: boolean }[] = [
  { name: "full_name", label: "Full name" },
  { name: "line1", label: "Address" },
  { name: "line2", label: "Address, line 2 (optional)", optional: true },
  { name: "postal_code", label: "Postal code" },
  { name: "city", label: "City" },
  { name: "country", label: "Country (two letters)" },
];

function Checkout({ reference, token }: { reference: string; token: string }) {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { color, space, fontSize } = useBrandTheme();
  const details = useQuery(orderDetailsQuery(client, reference, token));
  const [edited, setEdited] = useState<AddressFields | null>(null);
  const [problems, setProblems] = useState<AddressProblems>({});
  const [closed, setClosed] = useState(false);
  const fields = edited ?? fieldsOf(details.data?.shipping_address ?? null);

  const pay = useMutation({
    mutationFn: async (address: Parameters<typeof openCheckoutSession>[0]["address"]) => {
      const returnUrl = paymentReturnUrl();
      const redirectUrl = await openCheckoutSession({
        client,
        reference,
        token,
        address,
        returnUrl,
      });
      return payInBrowser(redirectUrl, returnUrl);
    },
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: orderKeys.all(reference) });
      if (result.type === "closed") {
        setClosed(true);
        return;
      }
      // On Android, the bridge page's link reaches the app as any link does, and
      // expo-router opens payment-return itself. On iOS, the auth session keeps
      // the link to itself and hands it over here.
      if (Platform.OS !== "android") {
        router.replace({
          pathname: "/payment-return",
          params: { status: result.status, reference: result.reference },
        });
      }
    },
  });

  if (details.isPending) {
    return (
      <ActivityIndicator
        style={{ margin: space.xl }}
        color={color.primary}
        accessibilityLabel="Loading the order"
      />
    );
  }
  if (details.isError) {
    return (
      <View style={{ padding: space.lg }}>
        <Notice
          tone="error"
          title="The order cannot be loaded"
          message={describeError(details.error)}
        />
      </View>
    );
  }
  const order = details.data;
  if (order.status !== "pending_payment") {
    return (
      <View style={{ padding: space.lg }}>
        <Notice
          title={order.status === "expired" ? "This order has expired" : "This order is paid"}
          message="There is nothing to pay."
          action={{
            label: "See the order",
            onPress: () => {
              router.back();
            },
          }}
        />
      </View>
    );
  }

  const submit = () => {
    const checked = validateAddress(fields);
    setProblems(checked.problems ?? {});
    setClosed(false);
    if (checked.address !== null) {
      pay.mutate(checked.address);
    }
  };

  return (
    <ScrollView
      style={{ backgroundColor: color.background }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      keyboardShouldPersistTaps="handled"
    >
      <Text style={{ color: color.text, fontSize: fontSize.body }}>
        {formatMoney(order.total_minor, order.currency)} for {order.product.name}, reserved until{" "}
        {formatDateTime(order.reserved_until)}. Where should it go?
      </Text>
      {FIELDS.map((field) => (
        <TextField
          key={field.name}
          label={field.label}
          value={fields[field.name]}
          onChangeText={(text) => {
            setEdited({ ...fields, [field.name]: text });
          }}
          autoCapitalize={field.name === "country" ? "characters" : "words"}
          autoComplete="off"
          error={problems[field.name]}
        />
      ))}
      {pay.isError ? (
        <Text accessibilityRole="alert" style={{ color: color.danger, fontSize: fontSize.body }}>
          {describeError(pay.error)}
        </Text>
      ) : null}
      {closed ? (
        <Notice
          title="The payment page was closed"
          message="The order is not paid yet. If you did pay, the order shows it within a minute."
        />
      ) : null}
      <Button
        label={`Pay ${formatMoney(order.total_minor, order.currency)}`}
        onPress={submit}
        busy={pay.isPending}
      />
    </ScrollView>
  );
}
