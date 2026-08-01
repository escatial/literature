/** 综述写作 hook:从文献池读文献 → 调后端 → 返回结果。*/
import { useState } from 'react';
import { postJSON } from '../api/client';
import type { WritingRequest, WritingResponse } from '../api/types';
import { db } from '../store/db';

export type ClassifyMode = 'locale' | 'theme';

interface UseWriterResult {
  generate: (classifyMode: ClassifyMode, doScreening: boolean) => Promise<void>;
  loading: boolean;
  error: string | null;
  result: WritingResponse | null;
}

export function useWriter(): UseWriterResult {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WritingResponse | null>(null);

  const generate = async (classifyMode: ClassifyMode, doScreening: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const topic = sessionStorage.getItem('lit_review_topic') ?? '';
      if (!topic) throw new Error('请先在"主题"页填写研究主题');

      // 只把选中的文献送去写作
      const selected = await db.papers.filter((p) => p.selected).toArray();
      if (selected.length === 0) throw new Error('文献池为空,请先完成英文检索/中文导入');

      // 映射到后端 PaperIn
      const papers = selected.map((p) => ({
        lit_id: p.lit_id,
        source: p.source,
        title: p.title,
        authors: p.authors,
        journal: p.journal,
        year: p.year,
        volume: p.volume,
        issue: p.issue,
        pages: p.pages,
        abstract: p.abstract,
        doi: p.doi,
        source_url: p.source_url,
        cited_by_count: p.cited_by_count ?? 0,
        journal_level: p.journal_level ?? null,
        relevance_score: p.relevance_score ?? null,
        raw_citation: p.raw_citation ?? null,
      }));

      const req: WritingRequest = {
        topic,
        papers,
        classify_mode: classifyMode,
        do_screening: doScreening,
      };
      const resp = await postJSON<WritingResponse>('/writing/generate', req);
      setResult(resp);

      // 持久化到 reviews 表(保留最近一份)
      await db.reviews.add({
        topic,
        classification: classifyMode === 'locale' ? 'by_locale' : 'by_theme',
        sections: resp.sections.map((s) => ({ title: s.title, content: s.content })),
        created_at: Date.now(),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return { generate, loading, error, result };
}
