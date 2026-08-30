import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Phase 18B：前端开发代理。
// /api/* 去掉 /api 前缀后转发（/api/sessions -> /sessions）；
// /chat/stream 原样转发（SSE 流式，禁止浏览器直连后端跨域）。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/chat/stream": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
