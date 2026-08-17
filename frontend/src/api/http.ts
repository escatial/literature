/** axios 实例 + 拦截器。*/
import axios, { AxiosError } from 'axios';
import { ElMessage } from 'element-plus';
import { getApiBaseURL, getBackendHint } from '@/config/api';

const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;
// dev 走 vite proxy, prod/自定义环境可通过 VITE_API_BASE 指向后端 origin。
const baseURL = getApiBaseURL(backendOrigin);

export const http = axios.create({
  baseURL,
  timeout: 180_000,
});

// 401/404 等客户端错误不弹 ElMessage(由调用方处理)
// 这里只对 5xx 或连接错误弹错,避免与未来 hooks 中 getQueryOptions 重复提示
http.interceptors.response.use(
  (resp) => resp,
  (err: AxiosError<{ detail?: string }>) => {
    const status = err.response?.status ?? 0;
    if (err.code === 'ERR_NETWORK' || status === 0) {
      // 浏览器拒绝连接/跨域/CORS;开发期 vite proxy 失败也会落到这里
      ElMessage.error(
        `无法连接后端 (${getBackendHint(backendOrigin)})。请确认已执行: cd backend && python -m uvicorn main:app --reload`,
      );
      return Promise.reject(new Error('ERR_NETWORK'));
    }
    const detail = err.response?.data?.detail ?? err.message ?? '网络错误';
    if (status >= 500) {
      ElMessage.error(`服务器错误: ${detail}`);
    }
    return Promise.reject(new Error(detail));
  },
);
