import { defineConfig, type Plugin, type ViteDevServer } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';
import fs from 'node:fs';
import { spawn, type ChildProcess } from 'node:child_process';
import { getBackendHint } from './src/config/api';

const apiTarget = getBackendHint(process.env.VITE_API_PROXY_TARGET);
const backendDir = path.resolve(__dirname, '../backend');

// 开发模式由 Vite 统一托管后端，避免只剩前端进程时 API 全部断连。
function backendSupervisor(): Plugin {
  let child: ChildProcess | null = null;
  let stopping = false;
  let restartTimer: NodeJS.Timeout | undefined;
  let healthMonitorTimer: NodeJS.Timeout | undefined;
  let restartAttempt = 0;

  const target = new URL(apiTarget);
  const isLocalBackend = ['127.0.0.1', 'localhost'].includes(target.hostname);
  const healthUrl = new URL('/api/healthz', target).toString();

  const isHealthy = async (): Promise<boolean> => {
    try {
      const response = await fetch(healthUrl, { signal: AbortSignal.timeout(1500) });
      return response.ok;
    } catch {
      return false;
    }
  };

  const startupTimeoutMs = Number(process.env.BACKEND_STARTUP_TIMEOUT_MS ?? 120_000);

  const waitUntilHealthy = async (timeoutMs = startupTimeoutMs): Promise<boolean> => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await isHealthy()) return true;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return false;
  };

  // 允许的后端 Python 解释器白名单:纯字面量候选路径表(逐个探测,取第一
  // 个存在的;全部不存在则抛错)。绝不读取 BACKEND_PYTHON / CONDA_PREFIX
  // 等环境变量——spawn 的可执行程序不允许被环境注入。dev 启动链路行为
  // 不变:第一个候选(literature 环境)在本机恒存在。端口/主机仍可经
  // VITE_API_PROXY_TARGET 覆盖(开发联调用途,spawn 前另有回环主机名与
  // 纯数字端口校验)。
  const allowedPythonPaths = [
    'D:\\Anaconda\\Anaconda\\envs\\literature\\python.exe',
    'D:\\Anaconda\\Anaconda\\python.exe',
  ];

  const resolvePython = (): string => {
    const found = allowedPythonPaths.find((candidate) => fs.existsSync(candidate));
    if (!found) {
      throw new Error('[backend] 未找到可用的后端 Python 解释器(候选路径均不存在)');
    }
    return found;
  };

  const scheduleRestart = () => {
    if (stopping || restartTimer) return;
    const delay = Math.min(1000 * (2 ** restartAttempt), 15_000);
    restartAttempt += 1;
    console.warn(`[backend] 已退出，${Math.ceil(delay / 1000)}s 后自动重启`);
    restartTimer = setTimeout(() => {
      restartTimer = undefined;
      void startBackend();
    }, delay);
  };

  const monitorHealth = () => {
    if (stopping || healthMonitorTimer) return;
    healthMonitorTimer = setInterval(() => {
      void isHealthy().then((healthy) => {
        if (!healthy || stopping || !child) return;
        restartAttempt = 0;
        console.log(`[backend] 健康检查通过: ${healthUrl}`);
        if (healthMonitorTimer) clearInterval(healthMonitorTimer);
        healthMonitorTimer = undefined;
      });
    }, 2000);
  };

  const startBackend = async (): Promise<void> => {
    if (!isLocalBackend || stopping || child || await isHealthy()) return;

    const python = resolvePython();
    // spawn 前显式收窄(静态可见):解释器必须命中字面量白名单、主机名必须
    // 为本机回环、端口必须为纯数字——三者任一越界立即抛错,不存在不可信
    // 程序选择或命令选项注入空间。
    const pythonAllowed = allowedPythonPaths.some(
      (candidate) => path.normalize(candidate).toLowerCase() === path.normalize(python).toLowerCase(),
    );
    if (!pythonAllowed) {
      throw new Error(`[backend] 拒绝启动白名单之外的 Python 解释器: ${python}`);
    }
    if (!['127.0.0.1', 'localhost'].includes(target.hostname)) {
      throw new Error(`[backend] 拒绝非本机回环的后端主机名: ${target.hostname}`);
    }
    const backendPort = /^\d{1,5}$/.test(target.port) ? target.port : '8000';
    console.log(`[backend] 启动 ${python} -m uvicorn main:app (${apiTarget})`);
    child = spawn(
      python,
      ['-m', 'uvicorn', 'main:app', '--host', target.hostname, '--port', backendPort],
      {
        cwd: backendDir,
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
        stdio: ['ignore', 'inherit', 'inherit'],
        windowsHide: true,
      },
    );
    child.once('error', (error) => {
      console.error('[backend] 启动失败:', error);
    });
    child.once('exit', (code, signal) => {
      child = null;
      if (healthMonitorTimer) clearInterval(healthMonitorTimer);
      healthMonitorTimer = undefined;
      if (!stopping) {
        console.error(`[backend] 进程退出 code=${code ?? '-'} signal=${signal ?? '-'}`);
        scheduleRestart();
      }
    });

    const checks = Math.ceil(startupTimeoutMs / 1000);
    for (let i = 0; i < checks; i += 1) {
      if (await isHealthy()) {
        restartAttempt = 0;
        console.log(`[backend] 健康检查通过: ${healthUrl}`);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (!child) return;
    }
    console.warn(`[backend] ${Math.round(startupTimeoutMs / 1000)}s 内未通过健康检查，后端仍在启动，将继续后台监测: ${healthUrl}`);
    monitorHealth();
  };

  const stopBackend = () => {
    stopping = true;
    if (restartTimer) clearTimeout(restartTimer);
    if (healthMonitorTimer) clearInterval(healthMonitorTimer);
    restartTimer = undefined;
    healthMonitorTimer = undefined;
    child?.kill();
    child = null;
  };

  return {
    name: 'local-backend-supervisor',
    apply: 'serve' as const,
    async configureServer(server: ViteDevServer) {
      // 注册在 Vite 内置 proxy 之前。后端冷启动或自动重启时先暂存 API
      // 请求，避免浏览器直接撞上尚未监听的 8000 端口并产生 ECONNREFUSED。
      if (isLocalBackend) {
        server.middlewares.use('/api', async (_req, res, next) => {
          if (await isHealthy() || await waitUntilHealthy()) {
            next();
            return;
          }
          res.statusCode = 503;
          res.setHeader('Content-Type', 'application/json; charset=utf-8');
          res.end(JSON.stringify({ detail: '后端仍在启动，请稍后重试' }));
        });
      }
      void startBackend();
      server.httpServer?.once('close', stopBackend);
    },
  };
}

export default defineConfig({
  plugins: [backendSupervisor(), vue()],
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
