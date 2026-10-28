import { defineConfig } from 'vitest/config';
import path from 'node:path';

/**
 * vitest 最小配置(D3):node 环境,不引 jsdom 全家桶。
 * - 只收 tests 目录下的 .spec.ts(纯逻辑/拦截器单测);
 * - alias '@' 与 vite.config.ts 保持一致;
 * - 不挂 backendSupervisor/vue 插件(单测不需要,加载更快)。
 */
export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.spec.ts'],
  },
});
