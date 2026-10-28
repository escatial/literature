/**
 * http.ts 响应拦截器单测(D3):401 清令牌跳登录 / 网络错误 toast 路径。
 * node 环境,不引 jsdom:window 以普通全局对象桩替;axios 拦截器取自
 * http.interceptors.response.handlers(axios 1.x 稳定结构)。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  setStoredToken: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock('@/api/auth', () => ({
  AUTH_TOKEN_KEY: 'lit_review.auth_token',
  setStoredToken: mocks.setStoredToken,
}));

vi.mock('@/utils/toast', () => ({
  toast: {
    success: vi.fn(),
    error: mocks.toastError,
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import { http } from '@/api/http';

type RejectHandler = (err: any) => Promise<any>;

function rejectionHandler(): RejectHandler {
  const handlers = (http.interceptors.response as any).handlers;
  expect(handlers.length).toBeGreaterThan(0);
  return handlers[handlers.length - 1].rejected as RejectHandler;
}

/** 构造最简 AxiosError 形状(拦截器只读 code/response/config/message)。 */
function axErr(over: Record<string, any>): any {
  return {
    code: undefined,
    response: undefined,
    config: undefined,
    message: 'Request failed',
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  // node 环境无 window:桩一个可变 hash 的普通对象(http.ts 只读写 hash)
  (globalThis as any).window = { location: { hash: '#/writing' } };
});

describe('401 处理', () => {
  it('业务接口 401:清 token 并跳转 #/login,原错误继续抛出', async () => {
    const err = axErr({
      response: { status: 401, data: {} },
      config: { url: '/reviews' },
    });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.setStoredToken).toHaveBeenCalledWith('');
    expect((globalThis as any).window.location.hash).toBe('#/login');
  });

  it('已在 #/login 时不重复改 hash', async () => {
    (globalThis as any).window.location.hash = '#/login';
    const err = axErr({
      response: { status: 401, data: {} },
      config: { url: '/reviews' },
    });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.setStoredToken).toHaveBeenCalledWith('');
    expect((globalThis as any).window.location.hash).toBe('#/login');
  });

  it('/auth/ 自身的 401 不清 token 不跳转(由登录页自行展示)', async () => {
    const err = axErr({
      response: { status: 401, data: { detail: '用户名或密码错误' } },
      config: { url: '/auth/login' },
    });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.setStoredToken).not.toHaveBeenCalled();
    expect((globalThis as any).window.location.hash).toBe('#/writing');
  });
});

describe('网络错误与 5xx', () => {
  it('ERR_NETWORK:弹「无法连接后端」toast,原错误继续抛出', async () => {
    const err = axErr({ code: 'ERR_NETWORK' });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.toastError).toHaveBeenCalledTimes(1);
    expect(String(mocks.toastError.mock.calls[0][0])).toContain('无法连接后端');
  });

  it('status 0(无响应):同样走 toast 路径', async () => {
    const err = axErr({ code: undefined, response: undefined });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.toastError).toHaveBeenCalledTimes(1);
  });

  it('5xx:弹服务器错误 toast,detail 写回 err.message', async () => {
    const err = axErr({
      response: { status: 500, data: { detail: '模型超时' } },
      config: { url: '/writing/generate-stream' },
      message: 'Internal Server Error',
    });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.toastError).toHaveBeenCalledTimes(1);
    expect(err.message).toBe('模型超时');
  });

  it('4xx(非401):不弹 toast,detail 写回 message(v9.6 语义)', async () => {
    const err = axErr({
      response: { status: 404, data: { detail: '任务不存在' } },
      config: { url: '/papers/xxx' },
      message: 'Not Found',
    });
    await expect(rejectionHandler()(err)).rejects.toBe(err);
    expect(mocks.toastError).not.toHaveBeenCalled();
    expect(err.message).toBe('任务不存在');
  });
});
