// First: zod's configuration must hold before any schema module is evaluated.
import "./zodConfig";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router";

import { App, createQueryClient } from "./app/App";
import { routes } from "./app/routes";
import { createServices } from "./app/services";
import "./app.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html must contain an element with id root");
}

createRoot(container).render(
  <StrictMode>
    <App
      services={createServices()}
      queryClient={createQueryClient()}
      router={createBrowserRouter(routes)}
    />
  </StrictMode>,
);
