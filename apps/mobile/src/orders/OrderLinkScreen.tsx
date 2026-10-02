import { useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";

import { useBrandTheme } from "../brand/brandTheme";
import { Notice } from "../ui/Notice";

import { storedOrdersKey, useOrderAccess } from "./OrderAccessProvider";
import { parseOrderReference, readLinkFragment } from "./reference";

/**
 * Where a buyer link opens the app: `<scheme>://link/LC-7K3F9Q#t=<token>`.
 * The token is read from the fragment (expo-router gives it as the `#`
 * param), kept in SecureStore, and the screen is replaced at once, so that
 * no route of the app keeps the link, the token in it, any longer.
 *
 * Expo Go does not register the brand's scheme: such links open the
 * development build only. "Add order" is the path that works everywhere.
 */
export function OrderLinkScreen() {
  const params = useLocalSearchParams<{ reference: string; "#"?: string }>();
  const store = useOrderAccess();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { color, space } = useBrandTheme();
  const [failed, setFailed] = useState(false);

  const reference = parseOrderReference(params.reference);
  const token = readLinkFragment(params["#"]);

  useEffect(() => {
    if (reference === null || token === null) {
      return;
    }
    let active = true;
    store
      .save(reference, token)
      .then(async () => {
        await queryClient.invalidateQueries({ queryKey: storedOrdersKey });
        if (active) {
          router.dismissTo("/orders");
        }
      })
      .catch(() => {
        if (active) {
          setFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [reference, token, store, queryClient, router]);

  if (reference === null || token === null || failed) {
    return (
      <View style={{ flex: 1, padding: space.lg, backgroundColor: color.background }}>
        <Notice
          tone="error"
          title="This link cannot be opened"
          message="It is not a complete order link. Add the order with its reference and token instead."
          action={{
            label: "Add an order",
            onPress: () => {
              router.replace("/orders/add");
            },
          }}
        />
      </View>
    );
  }
  return (
    <ActivityIndicator
      style={{ margin: space.xl }}
      color={color.primary}
      accessibilityLabel="Saving the order"
    />
  );
}
