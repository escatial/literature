/** 前后端联调相关的运行时配置。 */

const DEFAULT_BACKEND_ORIGIN = 'http://127.0.0.1:8000';

export function getBackendHint(origin?: string): string {
    return origin || DEFAULT_BACKEND_ORIGIN;
}

export function getApiBaseURL(origin?: string): string {
    return origin ? `${origin}/api` : '/api';
}

export function getWritingStreamURL(origin?: string): string {
    return `${getApiBaseURL(origin)}/writing/generate-stream`;
}

/** 阶段1:主题划分(筛选+分级+分类),停在确认点 */
export function getWritingPlanStreamURL(origin?: string): string {
    return `${getApiBaseURL(origin)}/writing/plan-stream`;
}
