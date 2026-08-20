import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built straight into the package the server ships, so `jobhunt serve` has
// something to serve without a copy step.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../jobhunt/web/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
