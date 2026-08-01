/** 文献池:查看/管理所有已入库文献,切换选中状态。*/
import { useLiveQuery } from 'dexie-react-hooks';
import { db, type Paper } from '../store/db';

function renderCitation(p: Paper): string {
  // 中文:用户粘贴的原始 GB/T 7714 引文
  if (p.source === 'user_imported' && p.raw_citation) return p.raw_citation;
  // 英文:平台元数据渲染
  const authors = p.authors.join(', ') || 'Anon';
  const vol = p.volume ? (p.issue ? `${p.volume}(${p.issue})` : p.volume) : '';
  const tail = [p.journal, p.year ? String(p.year) : '', vol]
    .filter(Boolean)
    .join(', ');
  const pages = p.pages ? `: ${p.pages}` : '';
  return `${authors}. ${p.title}[J]. ${tail}${pages}.`;
}

export function LiteraturePool() {
  const papers = useLiveQuery(() => db.papers.toArray(), [], [] as Paper[]);

  const toggle = async (lit_id: string, selected: boolean) => {
    await db.papers.update(lit_id, { selected: !selected });
  };
  const remove = async (lit_id: string) => {
    await db.papers.delete(lit_id);
  };
  const clearAll = async () => {
    if (confirm('确定清空文献池?')) await db.papers.clear();
  };

  const cn = papers.filter((p) => p.source === 'user_imported');
  const en = papers.filter((p) => p.source !== 'user_imported');
  const selectedCount = papers.filter((p) => p.selected).length;

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold">文献池</h1>
        <button
          onClick={clearAll}
          className="px-3 py-1.5 bg-red-600 text-white rounded text-sm hover:bg-red-700"
        >
          清空
        </button>
      </div>

      <div className="text-sm text-gray-600">
        共 {papers.length} 篇(中文 {cn.length} / 英文 {en.length}),已选{' '}
        {selectedCount} 篇
      </div>

      {papers.length === 0 ? (
        <div className="text-gray-500 text-sm">
          文献池为空,请先到"英文检索"或"中文导入"页添加文献。
        </div>
      ) : (
        <ul className="space-y-2">
          {papers.map((p) => (
            <li
              key={p.lit_id}
              className={`border rounded p-3 flex gap-3 items-start ${
                p.selected ? 'bg-white' : 'bg-gray-50 opacity-60'
              }`}
            >
              <input
                type="checkbox"
                checked={p.selected}
                onChange={() => toggle(p.lit_id, p.selected)}
                className="mt-1"
              />
              <div className="flex-1 min-w-0">
                <div className="flex gap-2 items-center flex-wrap">
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded ${
                      p.source === 'user_imported'
                        ? 'bg-orange-100 text-orange-700'
                        : 'bg-blue-100 text-blue-700'
                    }`}
                  >
                    {p.source === 'user_imported' ? '中文' : '英文'}
                  </span>
                  <span className="font-medium">{p.title}</span>
                  {p.year > 0 && (
                    <span className="text-xs text-gray-500">({p.year})</span>
                  )}
                </div>
                <div className="text-sm text-gray-600 mt-1 break-all">
                  {renderCitation(p)}
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  <code>{p.lit_id}</code>
                </div>
              </div>
              <button
                onClick={() => remove(p.lit_id)}
                className="text-red-600 hover:text-red-800 text-sm"
              >
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
