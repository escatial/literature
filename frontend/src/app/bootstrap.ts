/** 应用启动流程。 */

export interface BootstrapDeps {
    mountApp: () => void;
    clearPapers: () => Promise<void>;
}

export function bootstrapApp({ mountApp, clearPapers }: BootstrapDeps): void {
    // 修复:必须先等 clearPapers 完成再 mount,否则 WritingPage 的 fetchAll
    // 会在 clearPapers 之前返回(拿到旧数据)或之后返回(拿到空数据),
    // 造成「应用启动 → 自动清空 → 组件调」三者的 race condition。
    // 后端不可达时 2 秒超时兜底,避免卡死页面。
    void Promise.race([
        clearPapers(),
        new Promise((resolve) => setTimeout(resolve, 2000)),
    ])
        .catch(() => {
            // 后端未启动等场景直接放行
        })
        .finally(() => {
            mountApp();
        });
}

