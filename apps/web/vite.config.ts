import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// E2E runs serve the production build via `vite preview` and point the API
// proxy at the disposable E2E backend instead of the dev server on :8000.
const apiProxyTarget = process.env.E2E_API_ORIGIN ?? "http://127.0.0.1:8000";
const apiProxy = {
  "/api": { target: apiProxyTarget, changeOrigin: false },
  "/health": { target: apiProxyTarget, changeOrigin: false }
};

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
    proxy: apiProxy
  },
  preview: {
    proxy: apiProxy
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: [
      "e2e/**",
      "**/node_modules/**",
      "**/dist/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build,eslint,prettier}.config.*"
    ]
  }
});
