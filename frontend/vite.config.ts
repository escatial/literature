import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';
import { getBackendHint } from './src/config/api';

const apiTarget = getBackendHint(process.env.VITE_API_PROXY_TARGET);

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5174,
    // 显式绑 IPv4:Windows 下 Node 默认解析 localhost 到 ::1,只监听 IPv6 回环
    // 会导致浏览器访问 127.0.0.1:5174 失败(页面空白)
    host: '127.0.0.1',
    proxy: {
      '/api': {
        // 必须用 127.0.0.1:Windows 下 Node 解析 localhost 可能落到 ::1(IPv6),
        // 而后端 uvicorn 默认绑定 127.0.0.1:8000; 如需自定义可设 VITE_API_PROXY_TARGET
        target: apiTarget,
        changeOrigin: true,
        // 长 SSE 流关掉默认 30s 超时
        proxyTimeout: undefined,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['x-accel-buffering'] = 'no';
            proxyRes.headers['cache-control'] = 'no-cache';
          });
          proxy.on('error', (err) => {
            console.error('[vite proxy error]', err);
          });
        },
      },
    },
  },
});
