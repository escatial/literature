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

const TASK_ID_SESSION_KEY = 'lit_review.current_task_id';

// v9.6:任务 id 只从 sessionStorage 读取,关闭标签页后不恢复旧页面任务。
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const explicit = config.headers.get?.('X-Task-Id');
  if (explicit) return config;
  const taskId = sessionStorage.getItem(TASK_ID_SESSION_KEY);
  if (taskId) {
    config.headers.set('X-Task-Id', taskId);
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
