import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development /api goes to the production API at obehy.cz (OBEHY_API for another, such as a
// local `obehy api serve` on http://127.0.0.1:8000); in production nginx serves the app and /api/
// from one origin (deploy/nginx.conf).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.OBEHY_API ?? "https://obehy.cz",
        changeOrigin: true,
        secure: true,
      },
    },
  },
  build: {
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
