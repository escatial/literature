/** 与后端 API 对齐的类型定义。*/

export interface Paper {
  lit_id: string;
  source: 'openalex' | 'crossref' | 'user_imported' | 'cnki';
  title: string;
  authors: string[];
  journal: string;
  year: number;
  volume: string | null;
  issue: string | null;
  pages: string | null;
  abstract: string | null;
  doi: string | null;
  source_url: string;
  cited_by_count: number;
  journal_level?: string | null;
  relevance_score?: number | null;
  raw_citation?: string | null;
  quote_text?: string | null;
  abstract_text?: string | null;
  selected: boolean;
  created_at?: string;
}

export interface PaperCreatePayload extends Omit<Paper, 'created_at'> {}

// 需求5:文献池服务端分页响应
export interface PaperListResponse {
  items: Paper[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export type RetrievalTaskStatus = 'pending' | 'running' | 'succeeded' | 'failed';

export interface RetrievalTaskCreate {
  topic: string;
  year_start?: number;
  year_end?: number;
  min_citations: number;
  limit: number;
  use_rerank: boolean;
  /** 雪球扩展(引文回溯)独立开关;默认关闭 */
  use_snowball?: boolean;
  sources?: string[];
}

export interface RetrievalTaskCreated {
  task_id: string;
  status: RetrievalTaskStatus;
}

export interface RetrievalProgressEvent {
  stage: string;          // starting / source_ready / fetching_source / fetching_done / snowballing / filling / done ...
  source: string;         // openalex / pubmed / ''
  page: number;
  added: number;
  total: number;
  message: string;
  ts: string;             // ISO8601
}

export interface RetrievalTask {
  task_id: string;
  topic: string;
  status: RetrievalTaskStatus;
  progress: number;
  year_start: number;
  year_end: number;
  min_citations: number;
  limit: number;
  use_rerank: boolean;
  topic_summary: string;
  query_used: string;
  total_before_filter: number;
  total_after_filter: number;
  papers: Paper[];
  error: string | null;
  events: RetrievalProgressEvent[];
  created_at: string;
  updated_at: string;
}

export interface WritingSection {
  key: string;
  title: string;
  content: string;
  citations: string[];
}

export interface WritingGroup {
  name: string;
  lit_ids: string[];
}

export interface WritingRequest {
  topic: string;
  papers: Paper[];
  classify_mode: 'locale' | 'theme';
  do_screening: boolean;
}

export interface WritingResponse {
  topic: string;
  classify_mode: string;
  groups: WritingGroup[];
  sections: WritingSection[];
  reference_list: string;
  screened_out_ids: string[];
  dropped_citations: string[];
}

export interface ReviewRecord extends WritingResponse {
  id: number;
  created_at: string;
}
