/** axios 实例 + 拦截器。*/
import axios, { AxiosError } from 'axios';
import { ElMessage } from 'element-plus';

export const http = axios.create({
  baseURL: '/api',
  timeout: 180_000, // 写作类可能 1~3 分钟
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