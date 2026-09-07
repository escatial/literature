/** LLM Provider 运行时状态(供顶部状态条消费)。*/
import { defineStore } from 'pinia';
import {
    getLLMProviders,
    getLLMProvidersHealth,
    type LLMProviderHealth,
    type LLMProviderInfo,
} from '@/api/endpoints';

interface State {
    defaultProvider: string | null;
    fallbackOrder: string[];
    /** 已剔除未配 key provider 的真正执行顺序 */
    activeFallbackOrder: string[];
    providers: LLMProviderInfo[];
    health: Record<string, LLMProviderHealth>;
    lastRefreshedAt: number;
    loading: boolean;
    error: string | null;
}

export const useLLMProvidersStore = defineStore('llmProviders', {
    state: (): State => ({
        defaultProvider: null,
        fallbackOrder: [],
        activeFallbackOrder: [],
        providers: [],
        health: {},
        lastRefreshedAt: 0,
        loading: false,
        error: null,
    }),
    getters: {
        /** 当前正在生效的 provider(默认 = 一级,若无 key 则顺延 activeFallbackOrder[0])。*/
        currentProvider(state): string {
            if (state.activeFallbackOrder.length > 0) {
                return state.activeFallbackOrder[0];
            }
            return state.defaultProvider || '';
        },
        /** 任何一个 provider 处于"最近一次调用失败"时为 true,用于 UI 标红。*/
        anyProviderUnhealthy(state): boolean {
            return Object.values(state.health).some(
                h => h.called && h.healthy === false,
            );
        },
        /** 一级 provider 是否已配 key 但仍不可用,触发降级。*/
        firstProviderDegraded(state): boolean {
            const first = state.fallbackOrder[0];
            const activeFirst = state.activeFallbackOrder[0];
            return Boolean(
                first && activeFirst && first !== activeFirst,
            );
        },
    },
    actions: {
        async refresh() {
            this.loading = true;
            this.error = null;
            try {
                const [list, health] = await Promise.all([
                    getLLMProviders(),
                    getLLMProvidersHealth().catch(() => null),
                ]);
                this.defaultProvider = list.default;
                this.fallbackOrder = list.fallback_order;
                this.providers = list.providers;
                if (health) {
                    this.activeFallbackOrder = health.active_fallback_order;
                    this.health = health.providers;
                } else {
                    // 健康态接口挂了,fallback 到 list 的可读 provider 顺序
                    this.activeFallbackOrder = list.providers
                        .filter(p => p.available)
                        .map(p => p.id);
                }
                this.lastRefreshedAt = Date.now();
            } catch (e: any) {
                this.error = e?.message || String(e);
            } finally {
                this.loading = false;
            }
        },
        /** 让其它模块(比如 streaming 收到 provider_fallback 事件时)更新状态。*/
        applyHealthSnapshot(snapshot: {
            default?: string;
            fallback_order?: string[];
            active_fallback_order?: string[];
            providers?: Record<string, LLMProviderHealth>;
        }) {
            if (snapshot.default) this.defaultProvider = snapshot.default;
            if (snapshot.fallback_order) this.fallbackOrder = snapshot.fallback_order;
            if (snapshot.active_fallback_order)
                this.activeFallbackOrder = snapshot.active_fallback_order;
            if (snapshot.providers) this.health = snapshot.providers;
            this.lastRefreshedAt = Date.now();
        },
    },
});
