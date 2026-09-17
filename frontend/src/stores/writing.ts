/** 综述写作状态(全局单例)。
 *
 * 写作任务在 store 内运行,不依赖 WritingPage 组件存活:
 * - 切换到其他 tab 时组件被卸载,但 store 与 SSE 请求继续,
 *   后端任务不会中断,切回后仍可在当前应用会话内显示;
 * - 重复点击「开始写作」会被 running 拦截,避免并发重复请求;
 * - 提供 stop() 手动中止(AbortController);
 * - 写作状态仅保留在当前应用会话,重新打开页面从空白状态开始。
 */
import { defineStore } from 'pinia';
import { markRaw } from 'vue';
import { saveReview } from '@/api/endpoints';
import {
  generateWritingStream,
  planWritingStream,
  stopWritingRequest,
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
  qaProgress: null,
  qaResult: null,
  detail: null,
  error: null,
  activityLog: [],
};

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
    _runToken: 0,
  }),
  getters: {
    /** 是否有阶段性产出(>0 个章节或分组) */
    hasProgress: (s) =>
      s.stream.sections.length > 0 || s.stream.groups.length > 0,
  },
  actions: {
    reset() {
      // 重启或重新划分前，先中止仍在收尾的旧 SSE；仅递增 token
      // 只能阻止回调污染状态，不能停止后端继续消耗模型调用。
      this._controller?.abort();
      this.stream = { ...initialState };
      this.running = false;
      this.awaitingConfirm = false;
      this._planReq = null;
      this._controller = null;
      this._runToken += 1;
    },
    /** 阶段1:主题划分(筛选+相关性分级+分类)。跑到确认点即停,不写正文。 */
    async startPlan(req: WritingRequest) {
      if (this.running) return;
      this.reset();
      this.running = true;
      this.topic = req.topic;
      this._planReq = req;
      this._controller = markRaw(new AbortController());
      const runToken = this._runToken;
      const onUpdate = (s: StreamState) => {
        if (runToken !== this._runToken) return;
        this.stream = s;
        if (s.phase === 'await_confirm') {
          // 到达确认点:必须等用户确认才能进入下一步写作
          this.awaitingConfirm = true;
          // plan_complete 已经是阶段1的业务终点；无需等待 SSE 连接收尾才允许确认。
          this.running = false;
        }
      };
      try {
        await planWritingStream(req, onUpdate, this._controller?.signal);
      } catch (e: any) {
        // 用户主动停止,不当作错误
        if (e?.name === 'AbortError') {
          return;
        }
        if (runToken !== this._runToken) return;
        const msg = e?.message ?? String(e);
        this.stream = { ...this.stream, phase: 'error', error: msg, detail: msg };
      } finally {
        // 阶段1到达确认点后，旧 SSE 可能仍在收尾；不能清理阶段2刚建立
        // 的 controller，也不能把新任务的 running 状态改成 false。
        if (runToken === this._runToken) {
          this.running = false;
          this._controller = null;
        }
      }
    },
    /** 阶段2:用户确认(可编辑)主题分组后,按确认结果写正文。 */
    async confirmGroups(groups: WritingGroup[]) {
      if (this.running || !this.awaitingConfirm) return;
      const planReq = this._planReq;
      const plan = this.stream.plan;
      if (!planReq || !plan) return;
      this.awaitingConfirm = false;
      // 阶段1的确认流已经完成业务工作，进入阶段2前立即断开它，
      // 避免旧 SSE 继续占用连接并与新任务并发。
      this._controller?.abort();
      this.running = true;
      // 将阶段2视为新的运行代次，使阶段1 SSE 的迟到回调无法清理或覆盖它。
      this._runToken += 1;
      this._controller = markRaw(new AbortController());
      const runToken = this._runToken;
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
        if (runToken !== this._runToken) return;
        // 保留阶段1产出(plan/分级清单),阶段2事件里它们不会被重发
        this.stream = {
          ...s,
          plan: s.plan ?? plan,
          relevanceReport: s.relevanceReport ?? this.stream.relevanceReport,
        };
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
        if (runToken !== this._runToken) return;
        const msg = e?.message ?? String(e);
        this.stream = { ...this.stream, phase: 'error', error: msg, detail: msg };
      } finally {
        if (runToken === this._runToken) {
          this.running = false;
          this._controller = null;
        }
      }
    },
    /** 手动中止当前写作任务(后端 SSE 连接随之断开)。 */
    async stop() {
      // 先立即切断前端 SSE 并更新界面，不能等待后端网络请求返回；
      // 后端取消通知在后台发送，当前 LLM 调用结束后会协作式退出。
      this._controller?.abort();
      this._runToken += 1;
      this.running = false;
      this.stream = { ...this.stream, phase: 'idle', detail: '已停止写作任务' };
      void stopWritingRequest().catch(() => { /* 后端稍后自行收尾 */ });
    },
    /** 清除持久化的写作进度(用户主动点"清理"时使用) */
    clearProgress() {
      this.reset();
      this.topic = null;
    },
  },
});
