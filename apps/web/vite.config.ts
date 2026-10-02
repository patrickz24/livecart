import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API as compose publishes it, on the loopback interface only. 127.0.0.1
// rather than localhost: on Windows localhost resolves to ::1 first.
const API_ORIGIN = "http://127.0.0.1:8000";

export default defineConfig({
  // Tailwind CSS 4 reads its theme from src/app.css (@theme): no config file.
  plugins: [react(), tailwindcss()],
  server: {
    // Same origin as behind nginx: the browser only talks to this server, which
    // forwards API paths to FastAPI. No CORS, and SameSite=Strict cookies work.
    // Also used by `vite preview`.
    proxy: {
      // The versioned API surface (every route from milestone 3 on).
      "/api/": API_ORIGIN,
      // The liveness probe, outside /api/v1, that the shell page displays.
      "^/health$": API_ORIGIN,
    },
  },
  test: {
    environment: "jsdom",
  },
});
