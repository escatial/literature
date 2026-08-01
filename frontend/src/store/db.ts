/**
 * Dexie.js IndexedDB 数据库定义。
 *
 * - papers: 文献池(主键 lit_id)
 *   - 来源(英文): source = 'openalex' / 'crossref'
 *   - 来源(中文): source = 'user_imported',带 raw_citation 字段
 * - reviews: 综述记录
 * - topic: 主题(sessionStorage 也保留一份,便于刷新恢复)
 */
import Dexie, { type Table } from 'dexie';

export type PaperSource = 'openalex' | 'crossref' | 'user_imported';

export interface Paper {
  lit_id: string;
  source: PaperSource;
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
  cited_by_count?: number;
  journal_level?: string | null;
  relevance_score?: number | null;

  // 用户操作
  selected: boolean;
  raw_citation?: string | null;  // 中文:用户粘贴的原始 GB/T 7714 引文
  imported_at?: number;
}

export interface ReviewSection {
  title: string;
  content: string;
}

export interface Review {
  id?: number;
  topic: string;
  classification: 'by_locale' | 'by_theme';
  sections: ReviewSection[];
  created_at: number;
}

class LitReviewDB extends Dexie {
  papers!: Table<Paper, string>;
  reviews!: Table<Review, number>;

  constructor() {
    super('lit-review-db');
    this.version(1).stores({
      papers: 'lit_id, source, year, selected',
      reviews: '++id, topic, created_at',
    });
  }
}

export const db = new LitReviewDB();
