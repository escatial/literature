import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useRetriever } from '../hooks/useRetriever';
import { db } from '../store/db';
import type { Paper } from '../api/types';
import { FilterPanel } from '../components/FilterPanel';
import { PaperCard, paperToDb } from '../components/PaperCard';

export function EnglishRetrieval() {
  const topic = sessionStorage.getItem('lit_review_topic') || '';
  const [filters, setFilters] = useState({
    year_start: 2020,
    year_end: 2026,
    min_citations: 0,
    limit: 20,
    use_rerank: true,
  });
  const { loading, result, error, search } = useRetriever();
  const [savedCount, setSavedCount] = useState(0);

  useEffect(() => {
    if (topic) {
      search({ topic, ...filters });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic]);

  async function importSelected(papers: Paper[]) {
    const records = papers.map(paperToDb);
    await db.papers.bulkPut(records);
    setSavedCount((c) => c + records.length);
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">英文文献检索</h1>
      <p className="text-gray-600 mb-2">
        研究主题:<b>{topic}</b>
      </p>
      <FilterPanel filters={filters} onChange={setFilters} />
      <div className="flex gap-2 my-4">
        <button
          onClick={() => search({ topic, ...filters })}
          disabled={loading || !topic}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {loading ? '检索中(OpenAlex 慢时可能 1~2 分钟)...' : '开始检索'}
        </button>
        <Link to="/cn" className="px-4 py-2 bg-gray-200 rounded">
          下一步:中文导入 →
        </Link>
      </div>

      {error && <div className="text-red-600 mb-2">{error}</div>}
      {result && (
        <div className="mb-4 text-sm text-gray-700">
          检索式:<b>{result.query_used}</b>
          <br />
          平台原始命中:<b>{result.total_before_filter}</b> ·过滤后:
          <b>{result.total_after_filter}</b>
          {savedCount > 0 && (
            <>
              {' '}
              ·本会话入库:<b>{savedCount}</b>
            </>
          )}
        </div>
      )}

      <div>
        {result?.papers.map((p) => (
          <PaperCard key={p.lit_id} paper={p} onImport={() => importSelected([p])} />
        ))}
        {result && result.papers.length === 0 && !loading && (
          <div className="text-gray-500">无结果。尝试放宽过滤或换个关键词。</div>
        )}
      </div>

      {result && result.papers.length > 0 && (
        <button
          onClick={() => importSelected(result.papers)}
          className="my-4 px-4 py-2 bg-green-600 text-white rounded"
        >
          一键导入全部到文献池
        </button>
      )}
    </div>
  );
}
