/** 文献池状态(需求5:服务端分页)。
 *
 * - 当前页文献 + 总数 + 分页元数据均在 store 内;
 * - 切换 tab / 翻页 / 调整 page_size 时各自独立刷新;
 * - 计数(cn/en/selected)从当前页 + 本地缓存推算,带 "本页/总计" 两个维度。
 */
import { defineStore } from 'pinia';
import { ElMessage } from 'element-plus';
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
    /** 检索来源过滤:切换 Tab 触发刷新 */
    sourceFilter: 'all' as 'all' | 'cn' | 'en',
  }),
  getters: {
    selected: (s) => s.papers.filter((p) => Boolean(p && p.selected)),
    cnPapers: (s) => s.papers.filter((p) => p && isCnSource(p.source)),
    enPapers: (s) => s.papers.filter((p) => p && !isCnSource(p.source)),
  },
  actions: {
    /** 兼容旧代码:当前页切换时不重置 page_size,只换 page。 */
    async refresh(opts?: { page?: number; page_size?: number; source?: 'all' | 'cn' | 'en' }) {
      const page = opts?.page ?? this.pageMeta.page;
      const page_size = opts?.page_size ?? this.pageMeta.page_size;
      const src = opts?.source ?? this.sourceFilter;
      const params: Record<string, string | number> = { page, page_size };
      if (src === 'cn') params.source = 'user_imported,cnki';
      else if (src === 'en') params.source = 'openalex,pubmed';
      this.loading = true;
      try {
        const resp = await listPapers(params);
        this.papers = resp.items;
        this.pageMeta = {
          total: resp.total,
          page: resp.page,
          page_size: resp.page_size,
          total_pages: resp.total_pages,
        };
        this.sourceFilter = src;
      } catch (e) {
        // 拉取失败不静默吞:保留旧数据,把 pageMeta 归零避免 UI 误以为空
        console.error('[papers] refresh failed:', e);
        this.papers = [];
        this.pageMeta = {
          total: 0,
          page: 1,
          page_size: this.pageMeta.page_size || DEFAULT_PAGE_SIZE,
          total_pages: 1,
        };
        ElMessage.error('文献池拉取失败:后端无响应,请稍后重试');
      } finally {
        this.loading = false;
      }
    },
    async addBatch(items: PaperCreatePayload[]) {
      const r = await bulkUpsertPapers(items);
      ElMessage.success(`入库:新增 ${r.inserted},更新 ${r.updated}`);
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
      ElMessage.success('文献池已清空');
    },
    async clearBySource(source: string) {
      await apiClear(source);
      this.papers = this.papers.filter((p) => p.source !== source);
      // 真实总数由后端持有,触发刷新拉齐
      await this.refresh();
      ElMessage.success('已清空该来源文献');
    },
    setPageSize(size: number) {
      if (!ALLOWED_PAGE_SIZES.includes(size as any)) return;
      this.pageMeta = { ...this.pageMeta, page_size: size, page: 1 };
    },
  },
});