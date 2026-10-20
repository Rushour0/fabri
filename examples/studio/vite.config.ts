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
// The Spend section reads pba's ledger. Only /v1/spend is proxied, and the
// service token is added here so it never reaches the browser bundle.
const PBA = process.env.PBA_URL ?? "http://127.0.0.1:8790";
const PBA_HEADERS = {
  Authorization: `Bearer ${process.env.PBA_SERVICE_TOKEN ?? "local-dev-token"}`,
  "X-Company": process.env.PBA_COMPANY ?? "demo-league",
};

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
      "/setups": { target: FABRI_SERVE, changeOrigin: true },
      "/tools": { target: FABRI_SERVE, changeOrigin: true },
      "/health": { target: FABRI_SERVE, changeOrigin: true },
      // A regex key: exactly the two spend routes, so no other pba path gets the token.
      "^/pba/v1/spend(/summary)?(\\?.*)?$": { target: PBA, changeOrigin: true, headers: PBA_HEADERS, rewrite: (p) => p.replace(/^\/pba/, "") },
    },
  },
});
