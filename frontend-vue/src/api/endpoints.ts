/** 后端 RESTful API 客户端。*/
import { http } from './http';
import type {
  ImportCnResponse,
  Paper,
  PaperCreatePayload,
  QueryPlanResponse,
  RerankResponse,
  RetrievalTask,
  RetrievalTaskCreate,
  RetrievalTaskCreated,
  ReviewRecord,
  WritingRequest,
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
  query_en: string;
  field_zh: string;
  field_en: string;
}

export const queryPlan = (topic: string, yearStart: number) =>
  http.post<QueryPlanResponse>('/query-plan', { topic, year_start: yearStart }).then(r => r.data);

export const rerankPapers = (topic: string, papers: Paper[], topN = 50) =>
  http.post<RerankResponse>('/rerank', { topic, papers, top_n: topN }).then(r => r.data);

export const screeningFilter = (topic: string, papers: Paper[]) =>
  http.post<{ kept_ids: string[]; filtered_ids: string[] }>(
    '/screening/filter',
    { topic, papers },
  ).then(r => r.data);

export const createRetrievalTask = (req: RetrievalTaskCreate) =>
  http.post<RetrievalTaskCreated>('/retrieval/tasks', req).then(r => r.data);

export const getRetrievalTask = (taskId: string) =>
  http.get<RetrievalTask>(`/retrieval/tasks/${taskId}`).then(r => r.data);

export const listRetrievalTasks = () =>
  http.get<RetrievalTask[]>('/retrieval/tasks').then(r => r.data);

export const deleteRetrievalTask = (taskId: string) =>
  http.delete<{
    task_deleted: boolean;
    papers_deleted: number;
    task_status: string | null;
    papers_existed: number;
  }>(`/retrieval/tasks/${taskId}`).then(r => r.data);

export const generateWriting = (req: WritingRequest) =>
  http.post<WritingResponse>('/writing/generate', req).then(r => r.data);

// ─── 中文导入 ──────────────────────────────────────────────

export const parseChineseCitations = (rawText: string) =>
  http.post<ImportCnResponse>('/import/cn', { raw_text: rawText }).then(r => r.data);

// ─── 文献池 CRUD ───────────────────────────────────────────

export const listPapers = (params?: { source?: string; selected_only?: boolean }) =>
  http.get<Paper[]>('/papers', { params }).then(r => r.data);

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

export const listReviews = () =>
  http.get<ReviewRecord[]>('/reviews').then(r => r.data);

export const saveReview = (review: WritingResponse) =>
  http.post<ReviewRecord>('/reviews', review).then(r => r.data);