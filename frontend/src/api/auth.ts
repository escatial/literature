/** 认证与用户管理 API 客户端。令牌经 X-Auth-Token 头携带(http.ts 统一注入)。*/
import { http } from './http';

export const AUTH_TOKEN_KEY = 'lit_review.auth_token';

export interface AuthUser {
  user_id: string;
  username: string;
  email: string;
  role: 'admin' | 'user' | string;
  status: 'pending' | 'approved' | 'rejected' | string;
  created_at: string | null;
  /** 接口协议: openai(Chat Completions 兼容) / anthropic(Messages 兼容) */
  llm_kind: 'openai' | 'anthropic' | string;
  llm_base_url: string;
  llm_api_key_masked: string;
  llm_model: string;
}

export interface UpstreamModel {
  id: string;
  owned_by?: string;
  created?: number;
  active?: boolean;
}

export const authApi = {
  bootstrap: () => http.get<{ has_users: boolean }>('/auth/bootstrap'),
  register: (d: { username: string; password: string; email?: string }) =>
    http.post<{ status: string; role: string; message: string }>('/auth/register', d),
  login: (d: { username: string; password: string }) =>
    http.post<{ token: string; user: AuthUser }>('/auth/login', d),
  logout: () => http.post('/auth/logout'),
  me: () => http.get<{ user: AuthUser }>('/auth/me'),
  saveLlmConfig: (d: { llm_kind: string; llm_base_url: string; llm_api_key: string; llm_model: string }) =>
    http.put<{ user: AuthUser; message: string }>('/auth/llm-config', d),
  probeModels: (d: { llm_kind: string; llm_base_url: string; llm_api_key: string }) =>
    http.post<{ models: UpstreamModel[] }>('/auth/llm-config/models', d),
  adminUsers: () => http.get<{ users: AuthUser[] }>('/auth/admin/users'),
  approve: (userId: string) => http.post(`/auth/admin/users/${userId}/approve`),
  reject: (userId: string) => http.post(`/auth/admin/users/${userId}/reject`),
  removeUser: (userId: string) => http.delete(`/auth/admin/users/${userId}`),
};

export function getStoredToken(): string {
  return localStorage.getItem(AUTH_TOKEN_KEY) ?? '';
}

export function setStoredToken(token: string): void {
  if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
  else localStorage.removeItem(AUTH_TOKEN_KEY);
}
