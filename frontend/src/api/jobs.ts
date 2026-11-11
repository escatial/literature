/** J1(2026-11 任务化):写作任务 API 客户端。
 *
 * 任务在服务端独立运行,事件流经 /writing/jobs/{id}/stream 重放+实时 tail;
 * 本文件只负责任务的创建/确认/快照/停止,流消费在 api/streaming.ts。
 */
import { http } from './http';
import type { WritingGroup, WritingRequest } from './types';
import type { WritingJobInfo } from './streaming';

/** 启动写作任务(阶段1或携带 confirmed_groups 的阶段2),立即返回 job_id。 */
export const startWritingJob = (req: WritingRequest) =>
  http
    .post<{ job_id: string; status: string }>('/writing/jobs', req)
    .then((r) => r.data);

/** 阶段2确认:同一 job 内从确认点继续写正文(事件 seq 连续)。 */
export const confirmWritingJob = (jobId: string, groups: WritingGroup[]) =>
  http
    .post<{ job_id: string; status: string }>(
      `/writing/jobs/${jobId}/confirm`,
      { confirmed_groups: groups },
    )
    .then((r) => r.data);

/** 任务快照(status/phase/error/plan_groups/plan_papers);刷新恢复与重连前预检用。 */
export const getWritingJob = (jobId: string) =>
  http.get<WritingJobInfo>(`/writing/jobs/${jobId}`).then((r) => r.data);

/** 当前用户最新未完结任务(新标签页/浏览器重启后恢复用;sessionStorage 之外的第二恢复通道) */
export const getActiveWritingJob = () =>
  http
    .get<{ job: (WritingJobInfo & { plan_groups?: { name: string; lit_ids: string[] }[] | null; plan_papers?: { lit_id: string; title: string }[] | null }) | null }>(
      '/writing/jobs/active',
    )
    .then((r) => r.data.job);

/** 停止任务(后端协作式取消 + 落库)。 */
export const stopWritingJob = (jobId: string) =>
  http
    .post<{ job_id: string; status: string }>(`/writing/jobs/${jobId}/stop`)
    .then((r) => r.data);
