/** 前端直连 OpenAlex。 */
import type { Paper } from '@/api/types';

const OA_BASE = 'https://api.openalex.org/works';

function rebuildAbstract(inverted: Record<string, number[]> | null | undefined): string | null {
  if (!inverted) return null;
  const positions: Array<[number, string]> = [];
  for (const [word, pos] of Object.entries(inverted)) {
    for (const p of pos) positions.push([p, word]);
  }
  positions.sort((a, b) => a[0] - b[0]);
  const text = positions.map(([, w]) => w).join(' ').trim();
  return text || null;
}

export async function makeLitId(title: string | null, doi: string | null): Promise<string> {
  const raw = `${title || ''}|${doi || ''}`;
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
  const hex = Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
  return 'lit_' + hex.slice(0, 16);
}

interface OASearchParams {
  query: string;
  yearStart: number;
  yearEnd: number;
  perPage: number;
  mailto?: string;
}

export async function searchOpenAlex(p: OASearchParams): Promise<Paper[]> {
  const params = new URLSearchParams({
    search: p.query,
    filter: `publication_year:${p.yearStart}-${p.yearEnd}`,
    'per-page': String(Math.min(p.perPage, 200)),
    mailto: p.mailto ?? 'lit-review-agent@example.com',
  });
  const url = `${OA_BASE}?${params.toString()}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`OpenAlex 失败 ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = (await resp.json()) as { results?: any[] };
  const results = data.results ?? [];

  return Promise.all(
    results.map(async (w) => {
      const doiRaw: string = w.doi ?? '';
      const doi = doiRaw.replace(/^https?:\/\/doi\.org\//, '') || null;
      const title: string = (w.title || w.display_name || '').trim();
      const biblio = w.biblio ?? {};
      const volume = biblio.volume != null ? String(biblio.volume) : null;
      const issue = biblio.issue != null ? String(biblio.issue) : null;
      const first: string | null = biblio.first_page ?? null;
      const last: string | null = biblio.last_page ?? null;
      // 注意:字符串 '0' 或空字符串也应跳过
      const pages =
        first && last
          ? `${first}-${last}`
          : first ?? last ?? null;
      const primary = w.primary_location ?? {};
      const sourceLoc = primary.source ?? {};
      const journal: string = sourceLoc.display_name ?? '';
      const authors: string[] = (w.authorships ?? [])
        .filter((a: any) => a?.author?.display_name)
        .map((a: any) => a.author.display_name);

      return {
        lit_id: await makeLitId(title, doi),
        source: 'openalex' as const,
        title,
        authors,
        journal,
        year: w.publication_year ?? 0,
        volume,
        issue,
        pages,
        abstract: rebuildAbstract(w.abstract_inverted_index),
        doi,
        source_url: primary.landing_page_url ?? w.id ?? '',
        cited_by_count: w.cited_by_count ?? 0,
        selected: true,
      } satisfies Paper;
    })
  );
}
