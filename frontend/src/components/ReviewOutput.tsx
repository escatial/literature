/** 综述输出展示(含下载 Markdown)。*/
import type { WritingResponse } from '../api/types';
import { CitationList } from './CitationList';

interface Props {
  result: WritingResponse;
}

export function ReviewOutput({ result }: Props) {
  const downloadMarkdown = () => {
    const lines: string[] = [];
    lines.push(`# ${result.topic} 文献综述`);
    lines.push('');
    for (const s of result.sections) {
      lines.push(`## ${s.title}`);
      lines.push('');
      lines.push(s.content);
      lines.push('');
    }
    lines.push('## 参考文献');
    lines.push('');
    lines.push(result.reference_list);
    lines.push('');
    const blob = new Blob([lines.join('\n')], {
      type: 'text/markdown;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `文献综述-${result.topic}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-6">
      {result.screened_out_ids.length > 0 && (
        <div className="border-l-4 border-yellow-400 bg-yellow-50 p-3 text-sm">
          筛选阶段剔除了 {result.screened_out_ids.length} 篇主题不符文献:
          {result.screened_out_ids.join(', ')}
        </div>
      )}
      {result.dropped_citations.length > 0 && (
        <div className="border-l-4 border-red-400 bg-red-50 p-3 text-sm">
          检测到 {result.dropped_citations.length} 处幻觉引用已从正文剥离:
          {result.dropped_citations.join(', ')}
        </div>
      )}

      <div className="flex justify-between items-center">
        <h2 className="text-xl font-bold">{result.topic} 文献综述</h2>
        <button
          onClick={downloadMarkdown}
          className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
        >
          下载 Markdown
        </button>
      </div>

      {result.sections.map((s) => (
        <section key={s.key} className="border rounded p-4">
          <h3 className="font-semibold text-lg mb-2">{s.title}</h3>
          <div className="whitespace-pre-wrap text-sm leading-6">{s.content}</div>
        </section>
      ))}

      <section className="border rounded p-4 bg-gray-50">
        <h3 className="font-semibold text-lg mb-2">参考文献</h3>
        <pre className="whitespace-pre-wrap text-sm leading-6 font-sans">
          {result.reference_list || '无'}
        </pre>
      </section>

      <CitationList sections={result.sections} />
    </div>
  );
}
