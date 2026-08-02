/** 与后端 API 对齐的类型定义。*/

export interface Paper {
  lit_id: string;
  source: 'openalex' | 'crossref' | 'user_imported';
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
  selected: boolean;
  created_at?: string;
}

export interface PaperCreatePayload extends Omit<Paper, 'created_at'> {}

export interface ImportCitation {
  raw_text: string;
  authors: string;
  title: string;
  journal: string;
  year: number;
  volume: string | null;
  issue: string | null;
  pages: string | null;
  parsed_ok: boolean;
  error: string | null;
}

export interface ImportCnResponse {
  total: number;
  parsed_ok: number;
  parsed_fail: number;
  citations: ImportCitation[];
}

export interface QueryPlanResponse {
  topic_summary: string;
  keywords_en: string[];
  query_str: string;
}

export interface RerankResponse {
  papers: Paper[];
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