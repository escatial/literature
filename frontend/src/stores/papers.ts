/** 文献池状态(需求5:服务端分页)。
 *
 * - 当前页文献 + 总数 + 分页元数据均在 store 内;
 * - 切换 tab / 翻页 / 调整 page_size 时各自独立刷新;
 * - 计数(cn/en/selected)从当前页 + 本地缓存推算,带 "本页/总计" 两个维度。
 */
import { defineStore } from 'pinia';
import { toast } from '@/utils/toast';
import {
  bulkUpsertPapers,
  clearPapers as apiClear,
  deletePaper as apiDelete,
  listPapers,
  updatePaper,
} from '@/api/endpoints';
import type { Paper, PaperCreatePayload } from '@/api/types';

/** 中文来源:手动导入(user_imported)与知网自动检索(cnki)都算中文。 */
export const isCnSource = (source: Paper['source']): boolean =>
  source === 'user_imported' || source === 'cnki';

export const ALLOWED_PAGE_SIZES = [10, 20, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 20;

interface PageMeta {
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

/** 当前池中"已勾选去写作"的全部篇数(独立于 pageMeta,
 *  refresh 拉到失败时仍保留,显示给用户作为稳定入口) */
export interface PoolTotals {
  total: number;       // 池总数(走当前筛选)
  selected: number;    // 池中被 selected 的篇数
}

// v9.6:refresh 请求序号——只认最新一次请求的响应
let refreshSeq = 0;

export const usePapersStore = defineStore('papers', {
  state: () => ({
    papers: [] as Paper[],
    loading: false,
    pageMeta: {
      total: 0,
      page: 1,
      page_size: DEFAULT_PAGE_SIZE,
      total_pages: 1,
    } as PageMeta,
    /** 文献池总篇数(独立刷新:不受当前 page 切换影响,与 pageMeta.total 同步) */
    poolTotal: { total: 0, selected: 0 } as PoolTotals,
    /** 检索来源过滤:切换 Tab 触发刷新 */
    sourceFilter: 'all' as 'all' | 'cn' | 'en',
  }),
  getters: {
    selected: (s) => s.papers.filter((p) => Boolean(p && p.selected)),
    cnPapers: (s) => s.papers.filter((p) => p && isCnSource(p.source)),
    enPapers: (s) => s.papers.filter((p) => p && !isCnSource(p.source)),
  },
  actions: {
    /** 兼容旧代码:当前页切换时不重置 page_size,只换 page。
     * 同步刷新 pageMeta + poolTotal;失败时保留旧数据。
     * v9.6:请求序号保护——快速连续翻页时,旧请求晚到会用旧页数据覆盖新页。 */
    async refresh(opts?: {
      page?: number;
      page_size?: number;
      source?: 'all' | 'cn' | 'en';
    }) {
      const seq = ++refreshSeq;
      const page = opts?.page ?? this.pageMeta.page;
      const page_size = opts?.page_size ?? this.pageMeta.page_size;
      const src = opts?.source ?? this.sourceFilter;
      const params: Record<string, string | number> = { page, page_size };
      if (src === 'cn') params.source = 'user_imported,cnki';
      else if (src === 'en') params.source = 'openalex,pubmed';
      this.loading = true;
      try {
        const resp = await listPapers(params);
        if (seq !== refreshSeq) return; // 已有更新的请求,丢弃本次过期响应
        this.papers = resp.items;
        this.pageMeta = {
          total: resp.total,
          page: resp.page,
          page_size: resp.page_size,
          total_pages: resp.total_pages,
        };
        this.sourceFilter = src;
        // poolTotal 与 pageMeta 同步写 —— 避免"池空但显示 413"的不一致
        this.poolTotal = {
          total: resp.total,
          selected: resp.items.filter((p) => p && p.selected).length,
        };
      } catch (e) {
        if (seq !== refreshSeq) return;
        // 拉取失败:保留旧数据(不归零 pageMeta,避免 UI 误以为空)
        console.error('[papers] refresh failed:', e);
        toast.error('文献池拉取失败:请稍后重试');
      } finally {
        if (seq === refreshSeq) this.loading = false;
      }
    },
    async addBatch(items: PaperCreatePayload[]) {
      const r = await bulkUpsertPapers(items);
      toast.success(`入库:新增 ${r.inserted},更新 ${r.updated}`);
      await this.refresh();
    },
    /** 拉取文献池全部文献(自动翻页),供写作页使用,不依赖当前页勾选。 */
    async fetchAll(): Promise<Paper[]> {
      const all: Paper[] = [];
      let page = 1;
      const page_size = 100;
      for (;;) {
        const resp = await listPapers({ page, page_size });
        all.push(...resp.items);
        if (all.length >= resp.total || resp.items.length === 0) break;
        page += 1;
      }
      return all;
    },
    /** 拉取筛选后的文献(用于写作页的"筛选"快捷方式)。
     *  默认 selected_only=true,优先取已勾选;若 selected_only=false 则拉全部并截断到 limit。 */
    async fetchFiltered(opts: {
      selected_only?: boolean;
      limit?: number;
    } = {}): Promise<Paper[]> {
      const selectedOnly = opts.selected_only ?? true;
      const limit = opts.limit ?? 200;
      const all: Paper[] = [];
      let page = 1;
      const page_size = 100;
      for (;;) {
        const resp = await listPapers({
          page,
          page_size,
          selected_only: selectedOnly,
        });
        all.push(...resp.items);
        if (all.length >= resp.total || resp.items.length === 0) break;
        if (all.length >= limit) break;
        page += 1;
        if (page > 20) break; // 安全上限
      }
      return all.slice(0, limit);
    },
    /** 拉取"全部 selected"篇数(独立于 pageMeta),供「去写作(N 篇)」按钮展示 */
    async selectedCount(): Promise<number> {
      let total = 0;
      let page = 1;
      const page_size = 100;
      for (;;) {
        const resp = await listPapers({ page, page_size, selected_only: true });
        total += resp.items.length;
        if (allDone(resp)) break;
        page += 1;
      }
      return total;
      function allDone(r: { items: unknown[]; total: number }): boolean {
        return total >= r.total || r.items.length === 0;
      }
    },
    async toggle(paper: Paper) {
      await updatePaper(paper.lit_id, { selected: !paper.selected });
      paper.selected = !paper.selected;
    },
    async remove(litId: string) {
      await apiDelete(litId);
      this.papers = this.papers.filter((p) => p.lit_id !== litId);
      this.pageMeta.total = Math.max(0, this.pageMeta.total - 1);
    },
    async clearAll() {
      await apiClear();
      this.papers = [];
      this.pageMeta = { total: 0, page: 1, page_size: DEFAULT_PAGE_SIZE, total_pages: 1 };
      toast.success('文献池已清空');
    },
    async clearBySource(source: string) {
      await apiClear(source);
      this.papers = this.papers.filter((p) => p.source !== source);
      // 真实总数由后端持有,触发刷新拉齐
      await this.refresh();
      toast.success('已清空该来源文献');
    },
    setPageSize(size: number) {
      if (!ALLOWED_PAGE_SIZES.includes(size as any)) return;
      this.pageMeta = { ...this.pageMeta, page_size: size, page: 1 };
    },
  },
});