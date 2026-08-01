/** 后端 API 类型定义(与 backend/src/api/*.py 同步)。*/

export interface Paper {
  lit_id: string;
  source: string;
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
  relevance_score?: number | null;
}

export interface SearchRequest {
  topic: string;
  year_start: number;
  year_end: number;
  min_citations: number;
  limit: number;
  use_rerank: boolean;
}

export interface SearchResponse {
  topic_summary: string;
  query_used: string;
  total_before_filter: number;
  total_after_filter: number;
  papers: Paper[];
}

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

export interface WritingGroupOut {
  name: string;
  lit_ids: string[];
}

export interface WritingSectionOut {
  key: string;
  title: string;
  content: string;
  citations: string[];
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
  groups: WritingGroupOut[];
  sections: WritingSectionOut[];
  reference_list: string;
  screened_out_ids: string[];
  dropped_citations: string[];
}
