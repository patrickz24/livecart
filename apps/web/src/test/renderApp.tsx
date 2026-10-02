import { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { createMemoryRouter } from "react-router";

import { App } from "../app/App";
import { routes } from "../app/routes";
import { createServices } from "../app/services";
import type { FakeApi } from "./fakeApi";

/** The whole dashboard at `path`, its requests answered by `api`. */
export function renderApp(path: string, api: FakeApi) {
  const services = createServices({ send: api.send, locks: null });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const user = userEvent.setup();
  render(<App services={services} queryClient={queryClient} router={router} />);
  return { services, queryClient, router, user };
}
