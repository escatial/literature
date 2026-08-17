/** 后端 RESTful API 客户端。*/
import { http } from './http';
import type {
  Paper,
  PaperCreatePayload,
  PaperListResponse,
  RetrievalTask,
  RetrievalTaskCreate,
  RetrievalTaskCreated,
  ReviewRecord,
  WritingResponse,
} from './types';

// ─── 查询规划 / 重排 / 筛选 / 写作 ──────────────────────────

export interface ConceptGroup {
  id: string;
  label: string;
  label_en: string;
  synonyms_zh: string[];
  synonyms_en: string[];
}

export interface QueryPlanResponse {
  topic_summary: string;
  keywords_en: string[];
  keywords_zh: string[];
  query_str: string;
  concepts: ConceptGroup[];
  query_zh: string;
  queries_zh: string[];
  query_en: string;
  // 英文长检索式按语义单元拆分的子检索式列表(依次执行、合并去重)
  queries_en: string[];
  field_zh: string;
  field_en: string;
  // 三库方言检索式对比预览
  query_cnki: string;
  query_openalex: string;
  query_pubmed: string;
}

export const queryPlan = (topic: string, yearStart?: number) =>
  http.post<QueryPlanResponse>(
    '/query-plan',
    yearStart === undefined ? { topic } : { topic, year_start: yearStart },
  ).then(r => r.data);

export const createRetrievalTask = (req: RetrievalTaskCreate) =>
  http.post<RetrievalTaskCreated>('/retrieval/tasks', req).then(r => r.data);

export const getRetrievalTask = (taskId: string) =>
  http.get<RetrievalTask>(`/retrieval/tasks/${taskId}`).then(r => r.data);

// ─── 知网爬虫 全自动 / SSE ─────────────────────────────

export interface CnkiStartResponse {
  task_id: string;
  status: string;
  db_type: string;
}

export const startCnkiFullAuto = (req: {
  topic: string;
  expert_query: string;
  expert_queries: string[];
  target_count: number;
  max_pages?: number;
  db_type?: 'cnki';
}) =>
  http.post<CnkiStartResponse>('/cnki/start', req).then(r => r.data);

export const cnkiStreamUrl = (taskId: string) =>
  `${(import.meta as any).env?.VITE_API_BASE || ''}/api/cnki/stream/${taskId}`;

/** 停止检索任务(知网爬虫 + 英文 PubMed/OpenAlex)。
 *  taskIds 为空则后端停止所有运行中的任务。 */
export const stopRetrieval = (taskIds?: string[]) =>
  http.post<{ stopped: string[] }>(
    '/retrieval/stop',
    taskIds && taskIds.length ? { task_ids: taskIds } : {},
  ).then(r => r.data);

export const listRetrievalTasks = () =>
  http.get<RetrievalTask[]>('/retrieval/tasks').then(r => r.data);

export const deleteRetrievalTask = (taskId: string) =>
  http.delete<{
    task_deleted: boolean;
    papers_deleted: number;
    task_status: string | null;
    papers_existed: number;
  }>(`/retrieval/tasks/${taskId}`).then(r => r.data);

// ─── 文献池 CRUD(需求5:服务端分页) ─────────────────────────

export const listPapers = (params?: {
  source?: string;
  selected_only?: boolean;
  page?: number;
  page_size?: number;
}) =>
  http.get<PaperListResponse>('/papers', { params }).then(r => r.data);

export interface RetrievalHistory {
  id: number;
  topic: string;
  sources: string[];
  total_count: number;
  failed_sources: Record<string, number>;
  papers_snapshot?: Array<{
    lit_id: string;
    title: string;
    authors: string[];
    journal: string;
    year: number;
    source: string;
    doi: string;
  }>;
  task_id: string | null;
  created_at: string;
}

export const listRetrievalHistory = (limit = 5) =>
  http.get<RetrievalHistory[]>('/retrieval/history', { params: { limit } }).then(r => r.data);

/** 查看历史:把该条历史的文献快照恢复到文献池(先清空池再写入),返回恢复条数。 */
export const restoreRetrievalHistory = (historyId: number) =>
  http.post<{ total: number }>(
    `/retrieval/history/${historyId}/restore`,
    {},
  ).then(r => r.data);

/** 删除历史记录(连同其数据库中的快照数据)。 */
export const deleteRetrievalHistory = (historyId: number) =>
  http.delete(`/retrieval/history/${historyId}`).then(() => undefined);

export const bulkUpsertPapers = (papers: PaperCreatePayload[]) =>
  http.post<{ inserted: number; updated: number; skipped: number }>(
    '/papers/bulk',
    { papers },
  ).then(r => r.data);

export const updatePaper = (litId: string, patch: { selected?: boolean; relevance_score?: number }) =>
  http.patch<Paper>(`/papers/${litId}`, patch).then(r => r.data);

export const deletePaper = (litId: string) =>
  http.delete(`/papers/${litId}`).then(() => undefined);

export const clearPapers = (source?: string) =>
  http.delete('/papers', { params: source ? { source } : undefined }).then(() => undefined);

// ─── 综述历史 ──────────────────────────────────────────────

export const saveReview = (review: WritingResponse) =>
  http.post<ReviewRecord>('/reviews', review).then(r => r.data);
