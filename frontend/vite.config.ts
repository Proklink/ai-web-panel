import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// В режиме разработки (npm run dev) запросы к API проксируются на локальный бэкенд.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: { "/api": { target: "http://127.0.0.1:8100", ws: true } },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
