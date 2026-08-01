import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useImporter } from '../hooks/useImporter';
import { db, type Paper as DbPaper } from '../store/db';
import { hashId } from '../utils/hash';

export function ChineseImport() {
  const [text, setText] = useState('');
  const { loading, result, error, importText } = useImporter();
  const [savedCount, setSavedCount] = useState(0);

  async function saveAll() {
    if (!result) return;
    const papers: DbPaper[] = await Promise.all(
      result.citations
        .filter((c) => c.parsed_ok)
        .map(async (c) => ({
          lit_id: await hashId(c.raw_text),
          source: 'user_imported' as const,
          title: c.title,
          authors: c.authors
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean),
          journal: c.journal,
          year: c.year,
          volume: c.volume,
          issue: c.issue,
          pages: c.pages,
          abstract: null,
          doi: null,
          source_url: '',
          selected: true,
          raw_citation: c.raw_text,
          imported_at: Date.now(),
          journal_level: null,
        })),
    );
    await db.papers.bulkPut(papers);
    setSavedCount(papers.length);
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-4">中文文献批量导入</h1>
      <p className="text-gray-600 mb-2 text-sm">
        在知网查新(引文格式)选中多条,复制粘贴到下方。每行一条 GB/T 7714-2025 引文。
      </p>
      <textarea
        className="w-full p-2 border rounded font-mono text-sm"
        rows={12}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={'刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. 信息与电脑, 2025, 37(6): 227-229.\n另一条...'}
      />
      <div className="flex gap-2 mt-3">
        <button
          onClick={() => importText(text)}
          disabled={loading || !text.trim()}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {loading ? '解析中...' : '解析引文'}
        </button>
        {result && result.parsed_ok > 0 && (
          <button
            onClick={saveAll}
            className="px-4 py-2 bg-green-600 text-white rounded"
          >
            全部导入到文献池({result.parsed_ok})
          </button>
        )}
        <Link to="/pool" className="px-4 py-2 bg-gray-200 rounded">
          查看文献池 →
        </Link>
      </div>
      {error && <div className="text-red-600 mt-2">{error}</div>}
      {savedCount > 0 && (
        <div className="text-green-700 text-sm mt-2">本会话已入库 <b>{savedCount}</b> 条</div>
      )}
      {result && (
        <div className="mt-4 text-sm">
          共 <b>{result.total}</b> 条 ·解析成功{' '}
          <b className="text-green-700">{result.parsed_ok}</b>
          {result.parsed_fail > 0 && (
            <>
              {' '}·失败{' '}
              <b className="text-red-700">{result.parsed_fail}</b>
            </>
          )}
        </div>
      )}
      {result && (
        <table className="w-full text-sm mt-3 border">
          <thead className="bg-gray-100">
            <tr>
              <th className="p-1 text-left">作者</th>
              <th className="p-1 text-left">题名</th>
              <th className="p-1 text-left">刊名</th>
              <th className="p-1">年</th>
              <th className="p-1">卷(期)</th>
              <th className="p-1">页</th>
              <th className="p-1">状态</th>
            </tr>
          </thead>
          <tbody>
            {result.citations.map((c, i) => (
              <tr key={i} className="border-t">
                <td className="p-1">{c.authors || '—'}</td>
                <td className="p-1">{c.title || '—'}</td>
                <td className="p-1">{c.journal || '—'}</td>
                <td className="p-1 text-center">{c.year || '—'}</td>
                <td className="p-1 text-center">
                  {c.volume}
                  {c.issue && `(${c.issue})`}
                </td>
                <td className="p-1 text-center">{c.pages || '—'}</td>
                <td className="p-1 text-center">
                  {c.parsed_ok ? (
                    <span className="text-green-700">✓</span>
                  ) : (
                    <span className="text-red-600" title={c.error ?? ''}>
                      ✗
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
