/** 英文检索 hook:前端直连 OpenAlex,后端只做 LLM 拆词和重排。*/
import { useState } from 'react';
import { postJSON } from '../api/client';
import { searchOpenAlex } from '../api/openalex';
import type { Paper, SearchRequest, SearchResponse } from '../api/types';

interface QueryPlanResponse {
  topic_summary: string;
  keywords_en: string[];
  query_str: string;
}

interface RerankResponse {
  papers: Paper[];
}

/** 简单过滤(与后端 filters.py 一致逻辑)。*/
function applyFilters(
  papers: Paper[],
  yearStart: number,
  yearEnd: number,
  minCitations: number,
): Paper[] {
  return papers.filter(
    (p) =>
      p.year >= yearStart &&
      p.year <= yearEnd &&
      p.cited_by_count >= minCitations,
  );
}

/** DOI 去重 + (title, first_author, year) 复合键去重。*/
function deduplicate(papers: Paper[]): Paper[] {
  const seen = new Set<string>();
  const out: Paper[] = [];
  for (const p of papers) {
    const doiKey = p.doi ? `doi:${p.doi.toLowerCase()}` : '';
    const titleKey = `t:${p.title.toLowerCase()}|${(p.authors[0] || '').toLowerCase()}|${p.year}`;
    if (doiKey && seen.has(doiKey)) continue;
    if (seen.has(titleKey)) continue;
    if (doiKey) seen.add(doiKey);
    seen.add(titleKey);
    out.push(p);
  }
  return out;
}

export function useRetriever() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search(req: SearchRequest) {
    setLoading(true);
    setError(null);
    try {
      // 1. 后端 LLM 拆词
      const plan = await postJSON<QueryPlanResponse>('/query-plan', {
        topic: req.topic,
        year_start: req.year_start,
      });
      const query = plan.query_str || req.topic;

      // 2. 前端直连 OpenAlex
      const raw = await searchOpenAlex({
        query,
        yearStart: req.year_start,
        yearEnd: req.year_end,
        perPage: req.limit,
      });
      const totalBefore = raw.length;

      // 3. 前端过滤 + 去重
      let papers = applyFilters(raw, req.year_start, req.year_end, req.min_citations);
      papers = deduplicate(papers);

      // 4. 可选:后端 LLM 重排
      if (req.use_rerank && papers.length > 0) {
        try {
          const ranked = await postJSON<RerankResponse>('/rerank', {
            topic: plan.topic_summary,
            papers,
            top_n: Math.min(req.limit, 50),
          });
          papers = ranked.papers;
        } catch (e) {
          // 重排失败不阻塞,用原顺序
          console.warn('rerank 失败,使用原顺序', e);
        }
      }

      setResult({
        topic_summary: plan.topic_summary,
        query_used: query,
        total_before_filter: totalBefore,
        total_after_filter: papers.length,
        papers,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return { loading, result, error, search };
}
