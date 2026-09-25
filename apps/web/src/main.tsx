import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createApiClient } from "./api/client";
import { App } from "./app/App";
import "./app.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html must contain an element with id root");
}

const apiClient = createApiClient();

createRoot(container).render(
  <StrictMode>
    <App client={apiClient} />
  </StrictMode>,
);
