/** 引用清单展示。*/
import type { WritingSectionOut } from '../api/types';

interface Props {
  sections: WritingSectionOut[];
}

/** 提取正文中所有 [lit_xxx] 的出现顺序。*/
function extractCitations(text: string): string[] {
  const re = /\[(lit_[0-9a-f]{16})\]/g;
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (!out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

export function CitationList({ sections }: Props) {
  const rows = sections.map((s) => ({
    key: s.key,
    title: s.title,
    ids: extractCitations(s.content),
  }));
  return (
    <div className="border rounded p-4 bg-gray-50">
      <h3 className="font-semibold mb-2">引用清单</h3>
      {rows.map((r) => (
        <div key={r.key} className="mb-2 text-sm">
          <span className="font-medium">{r.title}:</span>
          {r.ids.length === 0 ? (
            <span className="text-gray-500 ml-2">无引用</span>
          ) : (
            <div className="flex flex-wrap gap-1 mt-1">
              {r.ids.map((id) => (
                <code
                  key={id}
                  className="px-1.5 py-0.5 bg-white border rounded text-xs"
                >
                  {id}
                </code>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
