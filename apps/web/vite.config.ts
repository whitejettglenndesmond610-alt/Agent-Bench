import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](echarts|zrender)[\\/]/.test(id)) return "echarts";
          if (/[\\/]node_modules[\\/](react-markdown|remark-[^\\/]+|rehype-[^\\/]+|lowlight|highlight\.js|micromark[^\\/]*|mdast-util-[^\\/]+|hast-util-[^\\/]+|unist-util-[^\\/]+|unified|vfile[^\\/]*|decode-named-character-reference|devlop|comma-separated-tokens|space-separated-tokens|property-information|trim-lines|markdown-table|ccount|escape-string-regexp|html-url-attributes|character-entities[^\\/]*|character-reference-invalid|estree-util-[^\\/]+|longest-streak|bail|trough|zwitch)[\\/]/.test(id)) return "markdown";
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler|react-router|react-router-dom|@tanstack[\\/][^\\/]+)[\\/]/.test(id)) return "vendor";
          return undefined;
        }
      }
    }
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: false }
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts"
  }
});
