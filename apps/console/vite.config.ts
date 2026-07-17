import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The console is served BY the engine daemon under /console, so the build must
// emit asset URLs relative to that base. dev proxies API calls to the local
// daemon (default 8786) so `npm run dev` talks to a real engine.
export default defineConfig({
  base: "/console/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5232,
    proxy: {
      // Every daemon route the console consumes. Same-origin in prod; proxied
      // in dev. Keep this list aligned with lib/api.ts endpoints.
      "^/(claims|stats|health|fail_mode_check|ground|principles|events|disposition|query|recall_stats|recall_slow_log|lifecycle|graph|audit_contradictions|subscribe)":
        {
          target: "http://127.0.0.1:8786",
          changeOrigin: true,
        },
    },
  },
  build: {
    outDir: "dist",
    // Code-split per route so the initial payload stays under budget.
    chunkSizeWarningLimit: 250,
  },
});
