import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Split heavy vendor libs into separate chunks so they can be cached
    // independently of app code and downloaded in parallel.
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("highlight.js")) return "vendor-hljs";
          if (id.includes("@xterm")) return "vendor-xterm";
          if (id.includes("/marked/") || id.includes("/dompurify/") || id.includes("/markdown-it/")) return "vendor-md";
          // Note: do NOT group mermaid/cytoscape/d3 into a single chunk —
          // Vite splits them per-diagram so they stay lazy. Forcing them into
          // one chunk makes the whole group eagerly loaded.
          if (id.includes("/react") || id.includes("/scheduler/")) return "vendor-react";
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://localhost:19099",
      "/ws": {
        target: "ws://localhost:19099",
        ws: true,
      },
    },
  },
});
