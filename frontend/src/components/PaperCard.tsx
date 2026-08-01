import type { Paper } from '../api/types';
import type { Paper as DbPaper } from '../store/db';

interface Props {
  paper: Paper;
  onImport?: () => void;
}

export function PaperCard({ paper: p, onImport }: Props) {
  return (
    <div className="border rounded p-3 mb-2">
      <div className="flex justify-between">
        <h3 className="font-semibold">{p.title}</h3>
        {p.relevance_score != null && (
          <span className="text-xs bg-blue-100 px-2 py-1 rounded">
            相关度:{p.relevance_score}
          </span>
        )}
      </div>
      <div className="text-sm text-gray-700 mt-1">
        {p.authors.slice(0, 3).join(', ')}
        {p.authors.length > 3 ? ', 等' : ''} · <i>{p.journal}</i>, {p.year}
        {p.volume ? `, ${p.volume}` : ''}
        {p.issue ? `(${p.issue})` : ''}
        {p.pages ? `: ${p.pages}` : ''}.
      </div>
      {p.abstract && (
        <details className="mt-2 text-sm text-gray-600">
          <summary className="cursor-pointer">摘要</summary>
          <p className="mt-1">
            {p.abstract.slice(0, 500)}
            {p.abstract.length > 500 ? '...' : ''}
          </p>
        </details>
      )}
      <div className="mt-2 flex gap-2 text-xs items-center">
        {p.source_url && (
          <a
            href={p.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 underline"
          >
            原文链接
          </a>
        )}
        {p.doi && <span className="text-gray-500">DOI: {p.doi}</span>}
        {p.cited_by_count > 0 && (
          <span className="text-gray-500">被引: {p.cited_by_count}</span>
        )}
        {onImport && (
          <button
            onClick={onImport}
            className="ml-auto px-2 py-0.5 bg-blue-600 text-white rounded"
          >
            导入
          </button>
        )}
      </div>
    </div>
  );
}

/** 把后端 Paper 类型转成 IndexedDB Paper 类型。 */
export function paperToDb(p: Paper): DbPaper {
  return {
    lit_id: p.lit_id,
    source: 'openalex',
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
    cited_by_count: p.cited_by_count,
    relevance_score: p.relevance_score ?? null,
    selected: true,
    imported_at: Date.now(),
    raw_citation: null,
    journal_level: null,
  };
}
