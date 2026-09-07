<script setup lang="ts">
/**
 * 全局吐司宿主:挂载在 App.vue 根部,渲染 utils/toast.ts 的通知队列。
 * 样式复刻自项目根目录 index.html 演示页(右上角滑入 + 进度条 + 手动关闭)。
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
                <div class="toast-icon icon-bounce">
                    <!-- 成功 -->
                    <svg v-if="t.type === 'success'" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
                    </svg>
                    <!-- 错误 -->
                    <svg v-else-if="t.type === 'error'" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    <!-- 警告 -->
                    <svg v-else-if="t.type === 'warning'" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <!-- 信息 -->
                    <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                </div>
                <div class="toast-content">
                    <div class="toast-title">{{ t.title }}</div>
                    <div class="toast-message">{{ t.message }}</div>
                </div>
                <button class="toast-close" aria-label="关闭" @click="dismissToast(t.id)">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
    top: 24px;
    right: 24px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 12px;
    pointer-events: none;
}

.toast {
    pointer-events: auto;
    min-width: 320px;
    max-width: 420px;
    padding: 16px 20px;
    border-radius: 12px;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    box-shadow:
        0 10px 40px rgba(0, 0, 0, 0.12),
        0 4px 12px rgba(0, 0, 0, 0.08);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    position: relative;
    overflow: hidden;
}

/* 进出场:右侧滑入/滑出(回弹曲线) */
.toast-enter-active,
.toast-leave-active {
    transition:
        transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1),
        opacity 0.4s ease;
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

/* 左侧彩色条 */
.toast::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
}

.toast.success {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(34, 197, 94, 0.2);
}
.toast.success::before {
    background: linear-gradient(180deg, #22c55e, #16a34a);
}

.toast.error {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(239, 68, 68, 0.2);
}
.toast.error::before {
    background: linear-gradient(180deg, #ef4444, #dc2626);
}

.toast.warning {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(245, 158, 11, 0.2);
}
.toast.warning::before {
    background: linear-gradient(180deg, #f59e0b, #d97706);
}

.toast.info {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(59, 130, 246, 0.2);
}
.toast.info::before {
    background: linear-gradient(180deg, #3b82f6, #2563eb);
}

.toast-icon {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
}

.toast.success .toast-icon {
    background: rgba(34, 197, 94, 0.1);
    color: #16a34a;
}

.toast.error .toast-icon {
    background: rgba(239, 68, 68, 0.1);
    color: #dc2626;
}

.toast.warning .toast-icon {
    background: rgba(245, 158, 11, 0.1);
    color: #d97706;
}

.toast.info .toast-icon {
    background: rgba(59, 130, 246, 0.1);
    color: #2563eb;
}

.toast-content {
    flex: 1;
    min-width: 0;
}

.toast-title {
    font-weight: 600;
    font-size: 14px;
    color: #111827;
    margin-bottom: 2px;
}

.toast-message {
    font-size: 13px;
    color: #6b7280;
    line-height: 1.5;
    overflow-wrap: break-word;
}

.toast-close {
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #9ca3af;
    cursor: pointer;
    border: none;
    background: transparent;
    padding: 0;
    border-radius: 4px;
    transition: all 0.2s ease;
}

.toast-close:hover {
    background: rgba(0, 0, 0, 0.05);
    color: #374151;
}

.toast-progress {
    position: absolute;
    bottom: 0;
    left: 0;
    height: 3px;
    background: currentColor;
    opacity: 0.3;
    width: 100%;
    transform-origin: left;
    animation: toast-progress linear forwards;
}

.toast.success .toast-progress { color: #22c55e; }
.toast.error .toast-progress { color: #ef4444; }
.toast.warning .toast-progress { color: #f59e0b; }
.toast.info .toast-progress { color: #3b82f6; }

@keyframes toast-progress {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
}

.icon-bounce {
    animation: icon-bounce 0.6s cubic-bezier(0.34, 1.56, 0.64, 1);
}

@keyframes icon-bounce {
    0% { transform: scale(0); }
    50% { transform: scale(1.2); }
    100% { transform: scale(1); }
}

@media (max-width: 640px) {
    .toast-container {
        top: 16px;
        right: 16px;
        left: 16px;
    }
    .toast {
        min-width: auto;
        max-width: none;
        width: 100%;
    }
}
</style>
