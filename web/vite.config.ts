import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes into the FastAPI static dir so `mercure-gateway --web`
// serves the SPA directly from localhost:8080 (ADR-0002 Method 1).
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
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
