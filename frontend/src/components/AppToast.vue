<script setup lang="ts">
/**
 * 全局吐司宿主:挂载在 App.vue 根部,渲染 utils/toast.ts 的通知队列。
 * 纸签样式:像夹在稿件上的便签,右上角滑入 + 进度条 + 手动关闭。
 */
import { toastList, dismissToast } from '@/utils/toast';
</script>

<template>
    <div class="toast-container">
        <TransitionGroup name="toast">
            <div
                v-for="t in toastList"
                :key="t.id"
                class="toast"
                :class="t.type"
                role="alert"
            >
                <div class="toast-mark" aria-hidden="true">
                    <!-- 成功 -->
                    <svg v-if="t.type === 'success'" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
                    </svg>
                    <!-- 错误 -->
                    <svg v-else-if="t.type === 'error'" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    <!-- 警告 -->
                    <svg v-else-if="t.type === 'warning'" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <!-- 信息 -->
                    <svg v-else fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                </div>
                <div class="toast-content">
                    <div class="toast-title">{{ t.title }}</div>
                    <div v-if="t.message" class="toast-message">{{ t.message }}</div>
                </div>
                <button class="toast-close" aria-label="关闭" @click="dismissToast(t.id)">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                </button>
                <div v-if="t.duration > 0" class="toast-progress" :style="{ animationDuration: `${t.duration}ms` }" />
            </div>
        </TransitionGroup>
    </div>
</template>

<style scoped>
.toast-container {
    position: fixed;
    top: 22px;
    right: 24px;
    z-index: 2147483001;
    display: flex;
    flex-direction: column;
    gap: 10px;
    pointer-events: none;
}

/* 纸签:暖白纸片、左缘墨色粗边、锐利小圆角 */
.toast {
    pointer-events: auto;
    min-width: 300px;
    max-width: 400px;
    padding: 13px 16px 15px;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: flex-start;
    gap: 11px;
    background: var(--sheet);
    border: 1px solid var(--rule-strong);
    box-shadow: var(--shadow-pop);
    position: relative;
    overflow: hidden;
}

/* 进出场:右侧滑入/滑出 */
.toast-enter-active,
.toast-leave-active {
    transition:
        transform 0.35s cubic-bezier(0.22, 1, 0.36, 1),
        opacity 0.3s ease;
}
.toast-enter-from,
.toast-leave-to {
    transform: translateX(120%);
    opacity: 0;
}
/* 退场中脱离文档流,让后续吐司平滑上移 */
.toast-leave-active {
    position: absolute;
    right: 0;
}

/* 左缘墨条:语义色 */
.toast::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
}
.toast.success::before { background: var(--ok); }
.toast.error::before { background: var(--bad); }
.toast.warning::before { background: var(--warn); }
.toast.info::before { background: var(--info); }

.toast-mark {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    margin-top: 1px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
}
.toast-mark svg {
    width: 14px;
    height: 14px;
}

.toast.success .toast-mark { background: var(--ok-bg); color: var(--ok); }
.toast.error .toast-mark { background: var(--bad-bg); color: var(--bad); }
.toast.warning .toast-mark { background: var(--warn-bg); color: var(--warn); }
.toast.info .toast-mark { background: var(--info-bg); color: var(--info); }

.toast-content {
    flex: 1;
    min-width: 0;
}

.toast-title {
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 13.5px;
    color: var(--ink);
    letter-spacing: 0.03em;
}

.toast-message {
    margin-top: 3px;
    font-size: 12.5px;
    color: var(--ink-soft);
    line-height: 1.6;
    overflow-wrap: break-word;
}

.toast-close {
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-ghost);
    cursor: pointer;
    border: none;
    background: transparent;
    padding: 0;
    border-radius: var(--radius-sm);
    transition: color 0.15s ease, background 0.15s ease;
}
.toast-close svg {
    width: 12px;
    height: 12px;
}
.toast-close:hover {
    background: var(--paper-deep);
    color: var(--ink);
}

.toast-progress {
    position: absolute;
    bottom: 0;
    left: 0;
    height: 2px;
    width: 100%;
    transform-origin: left;
    animation: toast-progress linear forwards;
}
.toast.success .toast-progress { background: var(--ok); }
.toast.error .toast-progress { background: var(--bad); }
.toast.warning .toast-progress { background: var(--warn); }
.toast.info .toast-progress { background: var(--info); }

@keyframes toast-progress {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
}

@media (max-width: 640px) {
    .toast-container {
        top: 12px;
        right: 12px;
        left: 12px;
    }
    .toast {
        min-width: auto;
        max-width: none;
        width: 100%;
    }
}
</style>
