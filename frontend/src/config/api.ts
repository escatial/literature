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
