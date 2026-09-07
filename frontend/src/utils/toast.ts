/**
 * 全局吐司通知(参照根目录 index.html 演示样式):
 * 右上角滑入、彩色侧条、图标弹跳、底部进度条、可手动关闭。
 * 用法:toast.success('已删除') / toast.error({ message: '...', duration: 0 })
 */
import { reactive } from 'vue';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastOptions {
    /** 标题,缺省按类型给默认标题 */
    title?: string;
    /** 正文消息 */
    message: string;
    /** 自动关闭毫秒数,0 表示不自动关闭 */
    duration?: number;
}

export interface ToastItem {
    id: number;
    type: ToastType;
    title: string;
    message: string;
    duration: number;
}

/** 响应式吐司队列,AppToast.vue 渲染用 */
export const toastList = reactive<ToastItem[]>([]);

let seed = 0;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

const DEFAULT_TITLES: Record<ToastType, string> = {
    success: '操作成功',
    error: '出现错误',
    warning: '请注意',
    info: '提示',
};

const DEFAULT_DURATION = 4000;

/** 关闭一条吐司(退场动画由 AppToast 的 TransitionGroup 处理) */
export function dismissToast(id: number): void {
    const timer = timers.get(id);
    if (timer) {
        clearTimeout(timer);
        timers.delete(id);
    }
    const idx = toastList.findIndex((t) => t.id === id);
    if (idx !== -1) toastList.splice(idx, 1);
}

function push(type: ToastType, options: ToastOptions | string): void {
    const opts: ToastOptions = typeof options === 'string' ? { message: options } : options;
    const item: ToastItem = {
        id: ++seed,
        type,
        title: opts.title ?? DEFAULT_TITLES[type],
        message: opts.message,
        duration: opts.duration ?? DEFAULT_DURATION,
    };
    toastList.push(item);
    if (item.duration > 0) {
        timers.set(item.id, setTimeout(() => dismissToast(item.id), item.duration));
    }
}

/** 全站统一提醒入口(替代 ElMessage) */
export const toast = {
    success: (options: ToastOptions | string) => push('success', options),
    error: (options: ToastOptions | string) => push('error', options),
    warning: (options: ToastOptions | string) => push('warning', options),
    info: (options: ToastOptions | string) => push('info', options),
};
