/** 登录会话 store:令牌持久化 localStorage,用户信息拉取自 /auth/me。*/
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import {
  authApi,
  getStoredToken,
  setStoredToken,
  type AuthUser,
} from '@/api/auth';
import { toast } from '@/utils/toast';

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(getStoredToken());
  const user = ref<AuthUser | null>(null);
  /** me 接口已确认过令牌有效(避免每个守卫都打一次后端) */
  const verified = ref(false);

  const isLoggedIn = computed(() => !!token.value);
  const isAdmin = computed(() => user.value?.role === 'admin');
  const username = computed(() => user.value?.username ?? '');

  async function login(username: string, password: string): Promise<void> {
    const { data } = await authApi.login({ username, password });
    token.value = data.token;
    user.value = data.user;
    verified.value = true;
    setStoredToken(data.token);
  }

  async function register(username: string, password: string, email?: string): Promise<string> {
    const { data } = await authApi.register({ username, password, email });
    return data.message;
  }

  /** 恢复会话:有令牌时校验一次;失效则静默清除(守卫会跳登录页)。 */
  async function restore(): Promise<boolean> {
    if (!token.value) return false;
    if (verified.value && user.value) return true;
    try {
      const { data } = await authApi.me();
      user.value = data.user;
      verified.value = true;
      return true;
    } catch {
      token.value = '';
      user.value = null;
      verified.value = false;
      setStoredToken('');
      return false;
    }
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout();
    } catch {
      /* 令牌已失效也照常登出 */
    }
    token.value = '';
    user.value = null;
    verified.value = false;
    setStoredToken('');
    toast.success('已退出登录');
  }

  function invalidate(): void {
    token.value = '';
    user.value = null;
    verified.value = false;
    setStoredToken('');
  }

  return {
    token, user, verified,
    isLoggedIn, isAdmin, username,
    login, register, restore, logout, invalidate,
  };
});
