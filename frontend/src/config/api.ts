/** 前后端联调相关的运行时配置。 */

const DEFAULT_BACKEND_ORIGIN = 'http://127.0.0.1:8000';

export function getBackendHint(origin?: string): string {
    return origin || DEFAULT_BACKEND_ORIGIN;
}

export function getApiBaseURL(origin?: string): string {
    return origin ? `${origin}/api` : '/api';
}


/** J1:写作任务事件流(可续传;since_seq 重放起点) */
export function getWritingJobStreamURL(origin: string | undefined, jobId: string, sinceSeq = 0): string {
  return `${getApiBaseURL(origin)}/writing/jobs/${jobId}/stream?since_seq=${sinceSeq}`;
}
