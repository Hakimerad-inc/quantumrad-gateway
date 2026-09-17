import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolveDevApiBase } from "./src/config/devApiBase";

// Build output goes into the FastAPI static dir so `mercure-gateway --web`
// serves the SPA directly from localhost:8080 (ADR-0002 Method 1).
//
// The dev proxy target resolves through resolveDevApiBase: 8080 by default,
// overridable via VITE_API_BASE_URL so `npm run dev` matches a backend that is
// not on the desktop default port (headless 8081, MERCURE_BACKEND_PORT).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/mercure_gateway/web/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: resolveDevApiBase(),
        changeOrigin: true,
      },
    },
  },
});
