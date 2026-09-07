/** 综述写作状态(全局单例)。
 *
 * 写作任务在 store 内运行,不依赖 WritingPage 组件存活:
 * - 切换到其他 tab 时组件被卸载,但 store 与 SSE 请求继续,
 *   后端任务不会中断,切回后自动恢复显示;
 * - 重复点击「开始写作」会被 running 拦截,避免并发重复请求;
 * - 提供 stop() 手动中止(AbortController);
 * - 每完成一节就把当前 stream 快照写到 localStorage,意外刷新/崩溃也能恢复。
 */
import { defineStore } from 'pinia';
import { markRaw } from 'vue';
import { saveReview } from '@/api/endpoints';
import {
  generateWritingStream,
  planWritingStream,
  type StreamState,
} from '@/api/streaming';
import type { WritingGroup, WritingRequest } from '@/api/types';

const initialState: StreamState = {
  phase: 'idle',
  sections: [],
  groups: [],
  referenceList: '',
  screenedOutIds: [],
  droppedCitations: [],
  relevanceReport: null,
  plan: null,
  progress: null,
  screeningProgress: null,
  elapsedSeconds: 0,
  waitingSeconds: 0,
  currentSection: null,
  detail: null,
  error: null,
};

const SNAPSHOT_KEY = 'writing.snapshot.v1';

interface Snapshot {
  topic: string | null;
  stream: StreamState;
  /** 阶段1请求参数:确认点刷新后恢复 confirmGroups 所需的 topic/classify_mode */
  planReq?: WritingRequest | null;
  updatedAt: number;
}

function loadSnapshot(): Snapshot | null {
  try {
    const raw = localStorage.getItem(SNAPSHOT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Snapshot;
    if (!parsed?.stream) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveSnapshot(topic: string, stream: StreamState, planReq?: WritingRequest | null) {
  // 只在「有产出」时持久化:避免每次 stream 变更都写
  const payload: Snapshot = {
    topic,
    stream: {
      ...stream,
      // currentSection.content 可能很长仍要存,便于刷新后展示已生成的章节
    },
    planReq: planReq ?? null,
    updatedAt: Date.now(),
  };
  try {
    localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(payload));
  } catch {
    // 配额满或隐私模式:忽略
  }
}

function clearSnapshot() {
  try {
    localStorage.removeItem(SNAPSHOT_KEY);
  } catch {
    // 忽略
  }
}

export const useWritingStore = defineStore('writing', {
  state: () => ({
    stream: { ...initialState } as StreamState,
    running: false,
    /** 阶段1完成:主题划分已产出,必须等用户确认后才允许开始写正文 */
    awaitingConfirm: false,
    /** 当前写作的任务主题(用于切 tab 后展示) */
    topic: null as string | null,
    /** 阶段1请求参数暂存(confirmGroups 需要 topic/classify_mode) */
    _planReq: null as WritingRequest | null,
    /** 当前写作请求的中止控制器(仅 startPlan/confirmGroups 期间有效) */
    _controller: null as AbortController | null,
  }),
  getters: {
    /** 是否有阶段性产出(>0 个章节或分组) */
    hasProgress: (s) =>
      s.stream.sections.length > 0 || s.stream.groups.length > 0,
  },
  actions: {
    /** 从 localStorage 恢复上一次写作快照(组件挂载时调用) */
    restore() {
      const snap = loadSnapshot();
      if (!snap) return;
      this.stream = snap.stream;
      this.topic = snap.topic;
      // 阶段1完成停在确认点:恢复后仍显示主题确认面板,用户确认后才进入阶段2
      this.awaitingConfirm = snap.stream.phase === 'await_confirm' && !!snap.stream.plan;
      this._planReq = (this.awaitingConfirm && snap.planReq) ? snap.planReq : null;
    },
    reset() {
      this.stream = { ...initialState };
      this.running = false;
      this.awaitingConfirm = false;
      this._planReq = null;
      this._controller = null;
      clearSnapshot();
    },
    /** 阶段1:主题划分(筛选+相关性分级+分类)。跑到确认点即停,不写正文。 */
    async startPlan(req: WritingRequest) {
      if (this.running) return;
      this.reset();
      this.running = true;
      this.topic = req.topic;
      this._planReq = req;
      this._controller = markRaw(new AbortController());
      const onUpdate = (s: StreamState) => {
        this.stream = s;
        // 阶段1产出(分组/主题方案)时持久化,刷新后可回到确认点
        if (s.groups.length > 0 || s.plan) {
          saveSnapshot(req.topic, s, req);
        }
        if (s.phase === 'await_confirm') {
          // 到达确认点:必须等用户确认才能进入下一步写作
          this.awaitingConfirm = true;
        }
      };
      try {
        await planWritingStream(req, onUpdate, this._controller?.signal);
      } catch (e: any) {
        // 用户主动停止,不当作错误
        if (e?.name === 'AbortError') {
          return;
        }
        const msg = e?.message ?? String(e);
        this.stream = { ...this.stream, phase: 'error', error: msg, detail: msg };
      } finally {
        this.running = false;
        this._controller = null;
      }
    },
    /** 阶段2:用户确认(可编辑)主题分组后,按确认结果写正文。 */
    async confirmGroups(groups: WritingGroup[]) {
      if (this.running || !this.awaitingConfirm) return;
      const planReq = this._planReq;
      const plan = this.stream.plan;
      if (!planReq || !plan) return;
      this.awaitingConfirm = false;
      this.running = true;
      this._controller = markRaw(new AbortController());
      // 阶段2契约:do_screening 必须为 false,papers 用阶段1筛选后的文献池
      const req: WritingRequest = {
        topic: planReq.topic,
        classify_mode: planReq.classify_mode,
        do_screening: false,
        papers: plan.papers,
        confirmed_groups: groups,
        relevance_report: this.stream.relevanceReport,
      };
      const onUpdate = (s: StreamState) => {
        // 保留阶段1产出(plan/分级清单),阶段2事件里它们不会被重发
        this.stream = {
          ...s,
          plan: s.plan ?? plan,
          relevanceReport: s.relevanceReport ?? this.stream.relevanceReport,
        };
        if (s.sections.length > 0 || s.groups.length > 0) {
          saveSnapshot(req.topic, this.stream);
        }
        if (s.phase === 'complete') {
          clearSnapshot();
        }
      };
      try {
        const finalResp = await generateWritingStream(
          req,
          onUpdate,
          this._controller?.signal,
        );
        await saveReview(finalResp);
        this._planReq = null;
      } catch (e: any) {
        // 用户主动停止,不当作错误
        if (e?.name === 'AbortError') {
          return;
        }
        const msg = e?.message ?? String(e);
        this.stream = { ...this.stream, phase: 'error', error: msg, detail: msg };
      } finally {
        this.running = false;
        this._controller = null;
      }
    },
    /** 手动中止当前写作任务(后端 SSE 连接随之断开)。 */
    stop() {
      this._controller?.abort();
    },
    /** 清除持久化的写作进度(用户主动点"清理"时使用) */
    clearProgress() {
      this.reset();
      this.topic = null;
    },
  },
});
