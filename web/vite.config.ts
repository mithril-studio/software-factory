import path from "node:path"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vite"

// Build to control/../web/dist; FastAPI serves it. In dev, proxy the API + SSE back to the
// FastAPI process on :8765 so the SPA hot-reloads against real data.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/healthz": { target: "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
})
