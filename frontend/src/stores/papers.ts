/** 文献池状态(与后端 SQLite 同步)。*/
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

export const usePapersStore = defineStore('papers', {
  state: () => ({
    papers: [] as Paper[],
    loading: false,
  }),
  getters: {
    selected: (s) => s.papers.filter((p) => p.selected),
    cnPapers: (s) => s.papers.filter((p) => p.source === 'user_imported'),
    enPapers: (s) => s.papers.filter((p) => p.source !== 'user_imported'),
  },
  actions: {
    async refresh() {
      this.loading = true;
      try {
        this.papers = await listPapers();
      } finally {
        this.loading = false;
      }
    },
    async addBatch(items: PaperCreatePayload[]) {
      const r = await bulkUpsertPapers(items);
      ElMessage.success(`入库:新增 ${r.inserted},更新 ${r.updated}`);
      await this.refresh();
    },
    async toggle(paper: Paper) {
      await updatePaper(paper.lit_id, { selected: !paper.selected });
      paper.selected = !paper.selected;
    },
    async remove(litId: string) {
      await apiDelete(litId);
      this.papers = this.papers.filter((p) => p.lit_id !== litId);
    },
    async clearAll() {
      await apiClear();
      this.papers = [];
      ElMessage.success('文献池已清空');
    },
  },
});