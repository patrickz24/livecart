import { describe, expect, it } from "@jest/globals";
import { renderRouter, screen } from "expo-router/testing-library";

import HomeScreen from "./app/index";

describe("the app", () => {
  it("opens on the home screen", async () => {
    await renderRouter({ index: HomeScreen }, { initialUrl: "/" });

    expect(screen.getByText("LiveCart")).toBeTruthy();
  });
});
