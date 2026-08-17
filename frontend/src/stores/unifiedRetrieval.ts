/** 统一检索页状态:每次进入页面均从空白任务开始。 */
import { defineStore } from 'pinia';
import type { ConceptGroup } from '@/api/endpoints';
import type { RetrievalProgressEvent } from '@/api/types';
import { readSharedTopic, writeSharedTopic } from './sharedTopic';

interface CnkiTaskRow {
  task_id: string;
  db_type: 'cnki';
  stage?: string;
  saved?: number;
  page_no?: number;
  total?: number;
  msg?: string;
  // 摘要进度(后端 fetched 事件携带);无界时 progress_total 不变,前端按 saved 推进
  progress_total?: number;
  progress_done?: number;
  // 检索过程日志(带时间戳前缀),按出现顺序追加,最多保留 200 条
  logs?: string[];
}

/** v4.1:英文任务按 db 拆的视图(对称中文 CnkiTaskRow) */
interface EnTaskRow {
  db: 'openalex' | 'pubmed';
  /** 关联到的英文任务 id(同任务的两个源共用) */
  task_id: string;
  stage?: string;
  /** 已入库(同上任务去重累计) */
  saved?: number;
  /** 当前源最近一次翻页返回的库总量 */
  libraryTotal?: number;
  /** 检索过程日志(后端事件拼成的人类可读字符串,带时间戳) */
  logs?: string[];
}

interface State {
  // 主题
  topic: string;
  // LLM 拆解 + 用户编辑
  concepts: ConceptGroup[];
  fieldZh: string;
  fieldEn: string;
  queryZh: string;
  queriesZh: string[];
  queryEn: string;
  // 英文长检索式按语义单元拆分后的子检索式列表(依次执行、合并去重)
  queriesEn: string[];
  // 三库方言检索式对比预览
  queryCnki: string;
  queryOpenalex: string;
  queryPubmed: string;
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
  // v4.0 知网任务列表(以 db 为 key,切 tab 仍保留)
  cnkiTasks: Record<string, CnkiTaskRow>;
  // v4.1 英文任务按 db 拆的视图(对称中文)
  enTasks: Record<'openalex' | 'pubmed', EnTaskRow>;
  // 已 ingest 的事件指纹(轮询全量事件去重,防止 saved/logs 重复累计)
  ingestedEnKeys: Set<string>;
  // 自动抽取相关
  autoTarget: number;
  // 知网翻页上限:每页 20 条,默认 3 页=60 条(覆盖大多数 VRP 这类中等主题;更多会触验证码)
  autoMaxPages: number;
}

const STORAGE_KEY = 'lit-review-unified-retrieval-v1';

const initial: State = {
  topic: readSharedTopic(),
  concepts: [],
  fieldZh: 'SU',
  fieldEn: 'default',
  queryZh: '',
  queriesZh: [],
  queryEn: '',
  queriesEn: [],
  queryCnki: '',
  queryOpenalex: '',
  queryPubmed: '',
  selectedDbs: ['cnki'],
  sessionId: '',
  dbTypes: [],
  currentIndex: 0,
  planning: false,
  extracting: false,
  chineseTaskId: '',
  englishTaskId: '',
  cnkiTasks: {},
  enTasks: { openalex: { db: 'openalex', task_id: '', logs: [] }, pubmed: { db: 'pubmed', task_id: '', logs: [] } },
  ingestedEnKeys: new Set<string>(),
  autoTarget: 300,
  // 知网翻页上限:20 页 × 20 条/页 = 400 条;None/0 视为"翻到知网无结果为止"
  autoMaxPages: 20,
};

function loadFromStorage(): Partial<State> {
  if (typeof localStorage === 'undefined') return {};
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    // Set 在 JSON 里退化为对象;恢复为 Set,避免去重指纹失效
    if (parsed.ingestedEnKeys && !(parsed.ingestedEnKeys instanceof Set)) {
      parsed.ingestedEnKeys = new Set(parsed.ingestedEnKeys);
    }
    return parsed;
  } catch {
    return {};
  }
}

function saveToStorage(s: State) {
  if (typeof localStorage === 'undefined') return;
  try {
    const payload = {
      ...s,
      ingestedEnKeys: Array.from(s.ingestedEnKeys || []),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
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
      this.ingestedEnKeys = new Set<string>();
      this.enTasks = {
        openalex: { db: 'openalex', task_id: '', logs: [] },
        pubmed: { db: 'pubmed', task_id: '', logs: [] },
      };
      this.persist();
    },

    /** 主题被改了,清掉旧的检索式结果。 */
    setTopic(t: string) {
      this.topic = t.trim();
      writeSharedTopic(this.topic);
      this.persist();
    },

    /** LLM 拆解结果写入,同时更新 queries。 */
    applyPlan(payload: {
      concepts: ConceptGroup[];
      field_zh: string;
      field_en: string;
      query_zh: string;
      queries_zh: string[];
      query_en: string;
      queries_en?: string[];
      query_cnki?: string;
      query_openalex?: string;
      query_pubmed?: string;
    }) {
      this.concepts = payload.concepts;
      this.fieldZh = payload.field_zh || 'SU';
      this.fieldEn = payload.field_en || 'default';
      this.queryZh = payload.query_zh;
      this.queriesZh = payload.queries_zh;
      this.queryEn = payload.query_en;
      this.queriesEn = payload.queries_en || [];
      this.queryCnki = payload.query_cnki || '';
      this.queryOpenalex = payload.query_openalex || '';
      this.queryPubmed = payload.query_pubmed || '';
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

    /** v4.0 知网任务状态写入(切 tab 后保留);不带 logs 时保留已有过程日志 */
    upsertCnkiTask(db: string, row: CnkiTaskRow) {
      const prev = this.cnkiTasks[db];
      this.cnkiTasks = {
        ...this.cnkiTasks,
        [db]: { ...row, logs: row.logs ?? prev?.logs },
      };
      this.persist();
    },
    /** 追加一条检索过程日志(带 HH:mm:ss 时间戳),限制条数防止无限膨胀 */
    appendCnkiLog(db: string, msg: string) {
      let row = this.cnkiTasks[db];
      if (!row) {
        // 兼容:row 不存在时(理论上 init 已建),就地建一个空任务
        row = { task_id: '', db_type: 'cnki', logs: [] };
        this.cnkiTasks = { ...this.cnkiTasks, [db]: row };
      }
      const ts = new Date().toTimeString().slice(0, 8);
      const logs = [...(row.logs ?? []), `${ts}  ${msg}`];
      this.cnkiTasks = {
        ...this.cnkiTasks,
        [db]: { ...row, logs: logs.slice(-200) },
      };
      this.persist();
    },
    /** 点击「启动自动检索」时立即写入占位任务行 + 一条启动日志,
     *  确保用户能在后端响应之前就看到过程面板。返回任务行供后续 upsert 覆盖。 */
    initCnkiTask(db: string, topic: string, dbType: 'cnki' = 'cnki') {
      const ts = new Date().toTimeString().slice(0, 8);
      const placeholder: CnkiTaskRow = {
        task_id: '',
        db_type: dbType,
        stage: 'starting',
        saved: 0,
        progress_done: 0,
        progress_total: 0,
        logs: [`${ts}  [本地] 已点击「启动自动检索」,主题=${JSON.stringify(topic)},等待后端响应…`],
      };
      this.cnkiTasks = { ...this.cnkiTasks, [db]: placeholder };
      this.persist();
      return placeholder;
    },
    clearCnkiTasks() {
      this.cnkiTasks = {};
      this.persist();
    },
    clearCnkiLogs(db: string) {
      const row = this.cnkiTasks[db];
      if (!row) return;
      this.cnkiTasks = { ...this.cnkiTasks, [db]: { ...row, logs: [] } };
      this.persist();
    },

    // === v4.1 英文两库枚举过程日志 ===

    /** 当英文任务启动时,把 task_id 写入两个 enTask 行,并清空日志与去重指纹。 */
    initEnTasks(taskId: string) {
      this.enTasks = {
        openalex: { db: 'openalex', task_id: taskId, logs: [] },
        pubmed: { db: 'pubmed', task_id: taskId, logs: [] },
      };
      this.ingestedEnKeys = new Set<string>();
      this.persist();
    },

    /** 把后端事件按 source 字段落到对应 enTask 的 logs 里。
     *  轮询会反复拿到同一批全量事件,用内容指纹去重,保证 saved 不重复累计。 */
    ingestEnEvents(taskId: string, events: RetrievalProgressEvent[]) {
      // 避免无关任务的旧事件污染
      if (!this.enTasks.openalex.task_id && !this.enTasks.pubmed.task_id) {
        this.initEnTasks(taskId);
      }
      const ts2hms = (iso: string) => {
        try {
          const d = new Date(iso);
          return d.toTimeString().slice(0, 8);
        } catch {
          return new Date().toTimeString().slice(0, 8);
        }
      };
      const fmt = (e: RetrievalProgressEvent): string => {
        const ts = ts2hms(e.ts);
        // 逐条命中日志:message 已含 [命中] 前缀,不再叠加 stage 标签(对称中文 [摘要])
        if (e.stage === 'paper_hit' && e.message) return `${ts}  ${e.message}`;
        if (e.message) return `${ts}  [${e.stage}] ${e.message}`;
        // 无 message 时拼装字段
        const tail = e.page ? ` 第 ${e.page} 页` : '';
        if (e.added || e.total) {
          return `${ts}  [${e.stage}] +${e.added} / 池 ${e.total}${tail}`;
        }
        return `${ts}  [${e.stage}]${tail}`;
      };
      for (const e of events) {
        if (!e.source) continue;
        if (e.source !== 'openalex' && e.source !== 'pubmed') continue;
        const row = this.enTasks[e.source];
        if (!row) continue;
        // 后端会反复推送同一批全量事件;用「索引+指纹」双重去重,避免放宽重检时误杀。
        const fingerprint = `${e.ts}|${e.stage}|${e.page}|${e.added}|${e.total}|${e.message}`;
        if (this.ingestedEnKeys.has(fingerprint)) continue;
        this.ingestedEnKeys.add(fingerprint);
        let saved = row.saved ?? 0;
        if (e.stage === 'fetching_source') {
          // 后端 total 是跨源共享池总量(会混入其他源的篇数),不能当单源 saved;
          // added 才是该源本页去重后真正新增数,累加即得该源实际入库数。
          if (typeof e.added === 'number' && e.added > 0) {
            saved += e.added;
          }
        } else if (e.stage === 'snowballing_done' && typeof e.added === 'number') {
          saved += e.added;
        } else if (e.stage === 'snowballing' && typeof e.total === 'number') {
          saved = Math.max(saved, e.total);
        }
        const logs = [...(row.logs ?? []), fmt(e)];
        this.enTasks = {
          ...this.enTasks,
          [e.source]: { ...row, saved, logs: logs.slice(-200) },
        };
      }
      this.persist();
    },

    /** 追加一条英文日志(本地事件,主要是任务开始前) */
    appendEnLog(db: 'openalex' | 'pubmed', msg: string) {
      const row = this.enTasks[db];
      if (!row) return;
      const ts = new Date().toTimeString().slice(0, 8);
      const logs = [...(row.logs ?? []), `${ts}  ${msg}`];
      this.enTasks = { ...this.enTasks, [db]: { ...row, logs: logs.slice(-200) } };
      this.persist();
    },

    /** 清空两个英文源的过程日志(同英文检索任务) */
    clearEnLogs() {
      this.enTasks = {
        openalex: { db: 'openalex', task_id: this.enTasks.openalex.task_id, logs: [] },
        pubmed: { db: 'pubmed', task_id: this.enTasks.pubmed.task_id, logs: [] },
      };
      this.persist();
    },
  },
});
