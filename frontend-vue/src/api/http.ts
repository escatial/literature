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

http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError<{ detail?: string }>) => {
    const detail =
      err.response?.data?.detail ?? err.message ?? '网络错误';
    ElMessage.error(`请求失败: ${detail}`);
    return Promise.reject(new Error(detail));
  },
);