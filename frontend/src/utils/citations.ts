// 把 LLM 生成的 [lit_xxx] 锚点转成可点击的脚注引用。
// content: LLM 原文(v-html 渲染前必须 escape 外部 HTML)
// allCitations: 本章可用的 lit_id 集合(用于区分有效/无效锚点)
// 返回已经 escape 过的 HTML 字符串,锚点变成 <sup>... 脚注形式

const _CITE_RE = /\[(lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]/g;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 把正文里的 [lit_xxx] 转成可点击脚注式引用:
 *   "张三(2024)认为 XYZ[lit_cnki_abc]。"
 * 转成:
 *   "张三(2024)认为 XYZ<sup class="cite-ref"><a href="#ref-lit_cnki_abc">[lit_cnki_abc]</a></sup>。"
 * allCitations 用于把有效锚点标深色、无效标灰。
 */
export function linkifyCitations(
  content: string,
  allCitations?: Set<string>,
): string {
  // 先整体 escape,再单独把锚点替换成 HTML(替换后再 escape 一下防止注入)
  return escapeHtml(content).replace(_CITE_RE, (full, litId) => {
    const valid = !allCitations || allCitations.has(litId);
    const cls = valid ? 'cite-ref cite-ref--valid' : 'cite-ref cite-ref--invalid';
    // title 用于悬浮显示具体引用编号
    return `<sup class="${cls}"><a href="#ref-${litId}" title="跳转参考文献 ${litId}">${litId}</a></sup>`;
  });
}

/**
 * 把参考文献列表加上脚注锚点 id,方便正文 [lit_xxx] 跳转。
 * 输入是 render_reference_list 输出的 GB/T 7714 列表(每行一条)。
 * 输出:每行加 `id="ref-lit_xxx"`,让 linkifyCitations 的锚点能跳过去。
 * 简化做法:取每个 `lit_xxx` 在文件中的第一次出现位置作为锚点。
 */
export function attachRefAnchors(refBlock: string): string {
  // 参考文献里形如 "<作者>. <题名>[J]. <刊名>, <年>..."
  // 我们用一个 map:lit_id -> 行号,然后插入 id="ref-xxx"
  // 但 render_reference_list 输出的纯文本没有 lit_id 信息,
  // 所以改由调用方传 lit_id 列表 + 标题列表组合。
  return refBlock;  // 占位,实际不使用(改由 downloadMd 在前端组装)
}

/**
 * 生成 markdown 脚注格式的 references 块。
 * 输入:papers 列表(每篇有 lit_id, 用于锚点) + 已经格式化好的 GB/T 7714 列表。
 * 输出:
 *   [^lit_xxx_1]: 张三. 题名[J]. 刊名, 2024, 1(1): 1-10.
 *   [^lit_xxx_2]: ...
 * 用于 md 文件下载,VS Code / GitHub 都能识别并跳转。
 */
export interface FootnotePaper {
  lit_id: string;
  citation: string;  // GB/T 7714 格式化好的条目(不含 lit_id 前缀)
}
export function toMarkdownFootnotes(refs: FootnotePaper[]): string {
  return refs
    .map((r) => `[^${r.lit_id}]: ${r.citation}`)
    .join('\n\n');
}
