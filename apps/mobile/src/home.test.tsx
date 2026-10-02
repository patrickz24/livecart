import { describe, expect, it } from "@jest/globals";
import { screen } from "expo-router/testing-library";

import HomeScreen from "./app/index";
import { renderRoutes } from "./test/renderRoutes";

describe("the app", () => {
  it("opens on the home screen, in its brand", async () => {
    await renderRoutes({ index: HomeScreen }, "/", { brandId: "pomelune" });

    expect(screen.getByText("Pomelune")).toBeTruthy();
  });
});
