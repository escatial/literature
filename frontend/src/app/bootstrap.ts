/** 应用启动流程。 */

export interface BootstrapDeps {
    mountApp: () => void;
    clearPapers: () => Promise<void>;
}

export function bootstrapApp({ mountApp, clearPapers }: BootstrapDeps): void {
    // 先挂载 UI，避免启动清理请求阻塞整页渲染。
    mountApp();
    void clearPapers().catch(() => {
        // 后端未启动等场景直接放行，页面自行提示。
    });
}

