import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiProxy = {
  "/api": {
    target: "http://127.0.0.1:8000",
    changeOrigin: false,
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
  build: {
    sourcemap: true,
    rollupOptions: {
      output: {
        // 数据与第三方库分别独立成 chunk：改代码时用户不必重新下载 2MB 赛题库，
        // KaTeX 只在打开方法库公式时才被请求。
        manualChunks(id) {
          if (id.includes("node_modules/katex")) return "katex";
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          return undefined;
        },
      },
    },
  },
});
