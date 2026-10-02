import { getOrderDetails } from "@livecart/api-client";
import { useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";

import { useApiClient } from "../api/ApiProvider";
import { describeError, isNotFound, unwrap } from "../api/errors";
import { useBrandTheme } from "../brand/brandTheme";
import { Notice } from "../ui/Notice";

import { storedOrdersKey, useOrderAccess } from "./OrderAccessProvider";
import { parseOrderReference, readLinkFragment } from "./reference";

/**
 * Where a buyer link opens the app: `<scheme>://link/LC-7K3F9Q#t=<token>`.
 * The token is read from the fragment (expo-router gives it as the `#`
 * param) and checked with the API, as "Add order" does, before SecureStore
 * keeps it: any app can open such a link, and an unchecked one could replace
 * the token of an order, or push real orders out of the store. The screen is
 * then replaced by the order's, so that no route of the app keeps the link,
 * the token in it, any longer.
 *
 * Expo Go does not register the brand's scheme: such links open the
 * development build only. "Add order" is the path that works everywhere.
 */
export function OrderLinkScreen() {
  const params = useLocalSearchParams<{ reference: string; "#"?: string }>();
  const client = useApiClient();
  const store = useOrderAccess();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { color, space } = useBrandTheme();
  const [failure, setFailure] = useState<{ error: unknown } | null>(null);
  const [attempt, setAttempt] = useState(0);

  const reference = parseOrderReference(params.reference);
  const token = readLinkFragment(params["#"]);

  useEffect(() => {
    if (reference === null || token === null) {
      return;
    }
    let active = true;
    unwrap(getOrderDetails({ client, path: { reference }, auth: token }))
      .then(() => store.save(reference, token))
      .then(async (saved) => {
        await queryClient.invalidateQueries({ queryKey: storedOrdersKey });
        if (active) {
          // Replaced, not pushed: the link, token included, leaves the stack.
          router.replace({ pathname: "/orders/[reference]", params: { reference: saved } });
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setFailure({ error });
        }
      });
    return () => {
      active = false;
    };
  }, [reference, token, attempt, client, store, queryClient, router]);

  const addOrder = {
    label: "Add an order",
    onPress: () => {
      router.replace("/orders/add");
    },
  };
  let notice = null;
  if (reference === null || token === null) {
    notice = (
      <Notice
        tone="error"
        title="This link cannot be opened"
        message="It is not a complete order link. Add the order with its reference and token instead."
        action={addOrder}
      />
    );
  } else if (failure !== null) {
    notice = isNotFound(failure.error) ? (
      <Notice
        tone="error"
        title="This link cannot be opened"
        message="The server knows no order with this link's reference and token."
        action={addOrder}
      />
    ) : (
      <Notice
        tone="error"
        title="This link cannot be opened"
        message={describeError(failure.error)}
        action={{
          label: "Try again",
          onPress: () => {
            setFailure(null);
            setAttempt((count) => count + 1);
          },
        }}
      />
    );
  }

  if (notice !== null) {
    return (
      <View style={{ flex: 1, padding: space.lg, backgroundColor: color.background }}>
        {notice}
      </View>
    );
  }
  return (
    <ActivityIndicator
      style={{ margin: space.xl }}
      color={color.primary}
      accessibilityLabel="Checking the order"
    />
  );
}
