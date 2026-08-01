interface Filters {
  year_start: number;
  year_end: number;
  min_citations: number;
  limit: number;
  use_rerank: boolean;
}

interface Props {
  filters: Filters;
  onChange: (f: Filters) => void;
}

export function FilterPanel({ filters, onChange }: Props) {
  function update(p: Partial<Filters>) {
    onChange({ ...filters, ...p });
  }
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 p-3 border rounded bg-gray-50">
      <label className="flex flex-col text-sm">
        年份起
        <input
          type="number"
          value={filters.year_start}
          onChange={(e) => update({ year_start: Number(e.target.value) })}
          className="border p-1 rounded"
        />
      </label>
      <label className="flex flex-col text-sm">
        年份止
        <input
          type="number"
          value={filters.year_end}
          onChange={(e) => update({ year_end: Number(e.target.value) })}
          className="border p-1 rounded"
        />
      </label>
      <label className="flex flex-col text-sm">
        最低被引
        <input
          type="number"
          value={filters.min_citations}
          onChange={(e) => update({ min_citations: Number(e.target.value) })}
          className="border p-1 rounded"
        />
      </label>
      <label className="flex flex-col text-sm">
        每源上限
        <input
          type="number"
          value={filters.limit}
          onChange={(e) => update({ limit: Number(e.target.value) })}
          className="border p-1 rounded"
        />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={filters.use_rerank}
          onChange={(e) => update({ use_rerank: e.target.checked })}
        />
        LLM 相关度重排
      </label>
    </div>
  );
}
