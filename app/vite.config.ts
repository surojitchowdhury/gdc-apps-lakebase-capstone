import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// FastAPI serves the built bundle as static files (see app/backend/main.py).
// Vite dev server proxies /api → uvicorn so `npm run dev` + `uvicorn` work side-by-side.
export default defineConfig({
  plugins: [react()],
  root: "frontend",
  build: {
    outDir: path.resolve(__dirname, "backend/static"),
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "frontend/src"),
    },
  },
});
