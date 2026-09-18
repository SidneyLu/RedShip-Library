import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  base: "./",
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    // AutoDL / container hosts often cannot raise inotify limits (ENOSPC).
    watch: {
      usePolling: true,
      interval: 1000,
    },
  },
  build: {
    outDir: "dist",
  },
});
