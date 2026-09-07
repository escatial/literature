/** axios 实例 + 拦截器。*/
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { toast } from '@/utils/toast';
import { getApiBaseURL, getBackendHint } from '@/config/api';

const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;
// dev 走 vite proxy, prod/自定义环境可通过 VITE_API_BASE 指向后端 origin。
const baseURL = getApiBaseURL(backendOrigin);

export const http = axios.create({
  baseURL,
  timeout: 180_000,
});

// v7.0 任务隔离:请求拦截器自动注入 X-Task-Id header。
// 这里只读 localStorage(同步可访问),Pinia store 也从同一处初始化。
// 优势:
//   1. 拦截器无需 import session store,避免循环依赖;
//   2. localStorage 是同步源,拦截器不依赖 Pinia 初始化时机;
//   3. store 与拦截器共享 LS_KEY,保持一致。
// 与 stores/session.ts 的 TASK_ID_LS_KEY 同步(避免重复 magic string)
const TASK_ID_LS_KEY = 'lit_review.current_task_id';
function readTaskIdFromLS(): string | null {
  try {
    return localStorage.getItem(TASK_ID_LS_KEY);
  } catch {
    return null;
  }
}
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  // 调用方已经显式带了 X-Task-Id 时,尊重调用方(用于「同 task 复用」语义)
  const explicit = config.headers.get?.('X-Task-Id');
  if (explicit) return config;
  // 否则从 localStorage 读取当前 task_id 注入
  const tid = readTaskIdFromLS();
  if (tid) {
    config.headers.set('X-Task-Id', tid);
  }
  return config;
});

// 401/404 等客户端错误不弹吐司(由调用方处理)
// 这里只对 5xx 或连接错误弹错,避免与未来 hooks 中 getQueryOptions 重复提示
http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError<{ detail?: string }>) => {
    const status = err.response?.status ?? 0;
    if (err.code === 'ERR_NETWORK' || status === 0) {
      // 浏览器拒绝连接/跨域/CORS;开发期 vite proxy 失败也会落到这里
      toast.error(
        `无法连接后端 (${getBackendHint(backendOrigin)})。请确认已执行: cd backend && python -m uvicorn main:app --reload`,
      );
      return Promise.reject(err);
    }
    const detail = err.response?.data?.detail ?? err.message ?? '网络错误';
    if (status >= 500) {
      toast.error(`服务器错误: ${detail}`);
    }
    // v9.6:不再把 AxiosError 换成普通 Error——原对象保留 status/response,
    // 调用方可区分 404(任务不存在)与瞬时抖动;detail 写回 message 保住既有文案
    err.message = detail;
    return Promise.reject(err);
  },
);
