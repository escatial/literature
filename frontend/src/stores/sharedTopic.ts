const SHARED_TOPIC_STORAGE_KEY = 'lit_review_topic';

export function readSharedTopic(): string {
    if (typeof sessionStorage === 'undefined') return '';
    try {
        return (sessionStorage.getItem(SHARED_TOPIC_STORAGE_KEY) ?? '').trim();
    } catch {
        return '';
    }
}

export function writeSharedTopic(topic: string): void {
    if (typeof sessionStorage === 'undefined') return;
    const normalized = topic.trim();
    try {
        if (normalized) {
            sessionStorage.setItem(SHARED_TOPIC_STORAGE_KEY, normalized);
        } else {
            sessionStorage.removeItem(SHARED_TOPIC_STORAGE_KEY);
        }
    } catch {
        // 浏览器存储异常时静默降级,不影响页面主流程
    }
}

export function applyHistoryTopic(
    topic: string,
    setTopic: (topic: string) => void,
): void {
    const normalized = topic.trim();
    if (!normalized) return;
    setTopic(normalized);
}

export { SHARED_TOPIC_STORAGE_KEY };
