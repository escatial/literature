/** axios 实例 + 拦截器。*/
import axios, { AxiosError } from 'axios';
import { ElMessage } from 'element-plus';

// 所有请求都走绝对地址,避开 vite proxy 的 SSE 缓冲问题
const baseURL = (import.meta as any).env?.VITE_API_BASE
  ? `${(import.meta as any).env.VITE_API_BASE}/api`
  : 'http://127.0.0.1:8080/api';

export const http = axios.create({
  baseURL,
  timeout: 180_000,
});

// 401/404 等客户端错误不弹 ElMessage(由调用方处理)
// 这里只对 5xx 弹错,避免与未来 hooks 中 getQueryOptions 重复提示
http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError<{ detail?: string }>) => {
    const detail =
      err.response?.data?.detail ?? err.message ?? '网络错误';
    // 后端仅在 4xx/5xx 设 detail;5xx 默认弹错
    const status = err.response?.status ?? 0;
    if (status >= 500) {
      ElMessage.error(`服务器错误: ${detail}`);
    }
    return Promise.reject(new Error(detail));
  },
);