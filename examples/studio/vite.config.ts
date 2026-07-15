import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Fabri Studio talks to a locally-running `fabri serve` (default 127.0.0.1:8080).
// The dev server proxies the API so the browser stays same-origin — `fabri serve`
// sets no CORS headers, and EventSource cannot send credentials cross-origin.
//
// SSE note: the `/runs/<id>/events` stream must NOT be buffered by the proxy or
// events arrive in a clump at the end. Vite's http-proxy streams responses as
// they arrive by default; we keep the connection alive and disable timeouts so a
// long-running agent's stream is never cut.
const FABRI_SERVE = process.env.FABRI_SERVE_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/runs": {
        target: FABRI_SERVE,
        changeOrigin: true,
        // Never time out an in-flight SSE stream or a blocking /result call.
        timeout: 0,
        proxyTimeout: 0,
      },
      "/fleets": { target: FABRI_SERVE, changeOrigin: true, timeout: 0, proxyTimeout: 0 },
      "/health": { target: FABRI_SERVE, changeOrigin: true },
    },
  },
});
