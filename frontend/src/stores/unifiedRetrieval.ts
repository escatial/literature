/** 统一检索页状态:每次进入页面均从空白任务开始。 */
import { defineStore } from 'pinia';
import type { ConceptGroup } from '@/api/endpoints';

interface State {
  // 主题
  topic: string;
  // LLM 拆解 + 用户编辑
  concepts: ConceptGroup[];
  fieldZh: string;
  fieldEn: string;
  queryZh: string;
  queryEn: string;
  // 选中的库
  selectedDbs: string[];
  // 会话
  sessionId: string;
  dbTypes: string[];
  currentIndex: number;
  // 异步任务:生成检索式是否在进行(切回时如果还在,会拉状态)
  planning: boolean;
  // 异步任务:抽取是否在进行
  extracting: boolean;
  chineseTaskId: string;
  englishTaskId: string;
  // 自动抽取相关
  autoTarget: number;
  autoMaxPages: number;
}

const STORAGE_KEY = 'lit-review-unified-retrieval-v1';

const initial: State = {
  topic: '',
  concepts: [],
  fieldZh: 'SU',
  fieldEn: 'default',
  queryZh: '',
  queryEn: '',
  selectedDbs: ['cnki', 'wanfang', 'cqvip'],
  sessionId: '',
  dbTypes: [],
  currentIndex: 0,
  planning: false,
  extracting: false,
  chineseTaskId: '',
  englishTaskId: '',
  autoTarget: 30,
  autoMaxPages: 10,
};

function loadFromStorage(): Partial<State> {
  if (typeof localStorage === 'undefined') return {};
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function saveToStorage(s: State) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch { /* ignore */ }
}

export const useUnifiedRetrievalStore = defineStore('unifiedRetrieval', {
  state: (): State => ({ ...initial }),

  actions: {
    /** 任何状态变更后调用,持久化。 */
    persist() {
      saveToStorage(this.$state as State);
    },

    reset() {
      Object.assign(this, initial);
      this.persist();
    },

    /** 主题被改了,清掉旧的检索式结果。 */
    setTopic(t: string) {
      this.topic = t;
      this.persist();
    },

    /** LLM 拆解结果写入,同时更新 queries。 */
    applyPlan(payload: {
      concepts: ConceptGroup[];
      field_zh: string;
      field_en: string;
      query_zh: string;
      query_en: string;
    }) {
      this.concepts = payload.concepts;
      this.fieldZh = payload.field_zh || 'SU';
      this.fieldEn = payload.field_en || 'default';
      this.queryZh = payload.query_zh;
      this.queryEn = payload.query_en;
      this.persist();
    },

    /** 用户改同义词后,重新计算 queries(并持久化)。 */
    setQueries(zh: string, en: string) {
      this.queryZh = zh;
      this.queryEn = en;
      this.persist();
    },

    setDbs(dbs: string[]) {
      this.selectedDbs = [...dbs];
      this.persist();
    },

    setSession(sid: string, dbTypes: string[], currentIndex: number) {
      this.sessionId = sid;
      this.dbTypes = dbTypes;
      this.currentIndex = currentIndex;
      this.persist();
    },

    clearSession() {
      this.sessionId = '';
      this.dbTypes = [];
      this.currentIndex = 0;
      this.persist();
    },

    setPlanning(v: boolean) { this.planning = v; this.persist(); },
    setExtracting(v: boolean) { this.extracting = v; this.persist(); },
    setAutoTarget(v: number) { this.autoTarget = v; this.persist(); },
    setAutoMaxPages(v: number) { this.autoMaxPages = v; this.persist(); },
  },
});