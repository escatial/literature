/** 写作页:分类方式 → 生成 → 展示。*/
import { useState } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { ClassifySelector } from '../components/ClassifySelector';
import { ReviewOutput } from '../components/ReviewOutput';
import { useWriter, type ClassifyMode } from '../hooks/useWriter';
import { db } from '../store/db';

export function Writing() {
  const [mode, setMode] = useState<ClassifyMode>('locale');
  const [doScreening, setDoScreening] = useState(true);
  const { generate, loading, error, result } = useWriter();

  const topic = sessionStorage.getItem('lit_review_topic') ?? '';
  const selectedCount = useLiveQuery(
    () => db.papers.filter((p) => p.selected).count(),
    [],
    0,
  );

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">综述写作</h1>

      <div className="border rounded p-4 space-y-3 bg-gray-50">
        <div className="text-sm">
          <span className="font-medium">研究主题:</span>
          <span className="ml-2">{topic || '(未设置,请回"主题"页填写)'}</span>
        </div>
        <div className="text-sm">
          <span className="font-medium">已选文献:</span>
          <span className="ml-2">{selectedCount} 篇</span>
        </div>
        <ClassifySelector value={mode} onChange={setMode} disabled={loading} />
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={doScreening}
            onChange={(e) => setDoScreening(e.target.checked)}
            disabled={loading}
          />
          写作前 LLM 筛选主题不符文献
        </label>
        <button
          onClick={() => generate(mode, doScreening)}
          disabled={loading || !topic || selectedCount === 0}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
        >
          {loading ? '生成中(可能需要 1~2 分钟)...' : '开始生成'}
        </button>
      </div>

      {error && (
        <div className="border-l-4 border-red-500 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {result && <ReviewOutput result={result} />}
    </div>
  );
}
