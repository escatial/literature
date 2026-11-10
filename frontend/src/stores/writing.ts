/** 综述写作状态(全局单例,J1 任务化版)。
 *
 * 任务在服务端独立运行(writing_jobs 表 + 可重放事件流),store 只是视图:
 * - 任何连接断开都以「全量重放」重连,状态无损重建 —— 刷新页面/切 tab/断网
 *   都不再丢失任务(经 resumeActive 接回);
 * - 单条事件流贯穿阶段1(到确认点)与阶段2(确认后续传),确认前后共用同一 job;
 * - stop() 同时中断本地消费与后端任务(协作式取消,落库)。
 */
import { defineStore } from 'pinia';
import { markRaw } from 'vue';
import { saveReview } from '@/api/endpoints';
import {
  confirmWritingJob,
  getWritingJob,
  startWritingJob,
  stopWritingJob,
} from '@/api/jobs';
import {
  buildWritingResponse,
  consumeWritingJobStream,
  type StreamState,
} from '@/api/streaming';
import type { WritingGroup, WritingRequest } from '@/api/types';

/** 活跃任务的 sessionStorage 键(刷新恢复入口) */
const ACTIVE_JOB_KEY = 'lit_review.writing_active_job';

// 工厂函数而非模块级常量:常量 + {...spread} 浅拷贝会让 sections/
// droppedCitations 等数组跨 reset 共享同一引用(与 api/streaming.ts v9.6
// 修复的污染 bug 同源隐患),每次 reset 都必须拿到全新内层数组。
function initialState(): StreamState {
  return {
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
    currentReasoning: '',
    sectionSteps: [],
    qaProgress: null,
    qaResult: null,
    detail: null,
    error: null,
    activityLog: [],
    harness: null,
    abstract: null,
    keywords: null,
    manifest: null,
    finalized: false,
  };
}

export const useWritingStore = defineStore('writing', {
  state: () => ({
    stream: initialState(),
    running: false,
    /** 阶段1完成:主题划分已产出,必须等用户确认后才允许开始写正文 */
    awaitingConfirm: false,
    /** 当前写作的任务主题(用于切 tab 后展示) */
    topic: null as string | null,
    /** 阶段1请求参数暂存(confirmGroups 需要 topic/classify_mode) */
    _planReq: null as WritingRequest | null,
    /** 当前活跃任务 id(J1;也是规划快照/权威落库的关联键) */
    jobId: null as string | null,
    /** 当前消费循环的中止控制器 */
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
      // 重新开始前:中断本地消费 + 停掉后端任务(仅递增 token 不能停止
      // 服务端继续消耗模型调用)。
      this._controller?.abort();
      if (this.jobId) {
        void stopWritingJob(this.jobId).catch(() => { /* 后端稍后自行收尾 */ });
        sessionStorage.removeItem(ACTIVE_JOB_KEY);
      }
      this.stream = initialState();
      this.running = false;
      this.awaitingConfirm = false;
      this._planReq = null;
      this._controller = null;
      this.jobId = null;
      this._runToken += 1;
    },

    /** 阶段1:创建任务并消费事件流,跑到确认点即停(不写正文)。 */
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
          this.awaitingConfirm = true;
          this.running = false;
        }
      };
      try {
        const job = await startWritingJob(req);
        if (runToken !== this._runToken) return;
        this.jobId = job.job_id;
        sessionStorage.setItem(ACTIVE_JOB_KEY, job.job_id);
        const st = await consumeWritingJobStream({
          jobId: job.job_id,
          onUpdate,
          signal: this._controller?.signal,
        });
        if (st.phase === 'await_confirm') {
          this.awaitingConfirm = true;
        }
      } catch (e: any) {
        if (e?.name === 'AbortError') return;
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

    /** 阶段2:确认主题分组后,同一任务从确认点续写(事件 seq 连续)。 */
    async confirmGroups(groups: WritingGroup[]) {
      if (this.running || !this.awaitingConfirm) return;
      const planReq = this._planReq;
      const jobId = this.jobId;
      const prevStream = this.stream;
      if (!planReq || !jobId) return;
      this.awaitingConfirm = false;
      this.running = true;
      this._runToken += 1;
      this._controller = markRaw(new AbortController());
      const runToken = this._runToken;
      const onUpdate = (s: StreamState) => {
        if (runToken !== this._runToken) return;
        // 全量重放会重建阶段1产出(plan/分级清单);这里仍做一层保底合并
        this.stream = {
          ...s,
          plan: s.plan ?? prevStream.plan,
          relevanceReport: s.relevanceReport ?? prevStream.relevanceReport,
        };
      };
      try {
        await confirmWritingJob(jobId, groups);
        if (runToken !== this._runToken) return;
        const st = await consumeWritingJobStream({
          jobId,
          onUpdate,
          signal: this._controller?.signal,
          initialState: prevStream,
        });
        // D1 完成性门禁:manifest 校验不过一律抛错,残品绝不入库
        const finalResp = buildWritingResponse(st, planReq.topic, planReq.classify_mode, groups);
        await saveReview(finalResp);
        this._planReq = null;
        sessionStorage.removeItem(ACTIVE_JOB_KEY);
      } catch (e: any) {
        if (e?.name === 'AbortError') return;
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

    /** 手动中止:断开本地消费 + 停掉后端任务(协作式取消,落库)。 */
    async stop() {
      this._controller?.abort();
      this._runToken += 1;
      this.running = false;
      this.stream = { ...this.stream, phase: 'idle', detail: '已停止写作任务' };
      if (this.jobId) {
        const jobId = this.jobId;
        void stopWritingJob(jobId).catch(() => { /* 后端稍后自行收尾 */ });
      }
    },

    /** J1:刷新/重开页面后恢复活跃任务的界面(全量重放,状态无损重建)。
     *  WritingPage 挂载时调用;running/awaiting_confirm 才恢复。 */
    async resumeActive() {
      if (this.running || this.jobId) return;
      const jobId = sessionStorage.getItem(ACTIVE_JOB_KEY);
      if (!jobId) return;
      let job;
      try {
        job = await getWritingJob(jobId);
      } catch {
        // 404/清理:活跃键失效,静默清除
        sessionStorage.removeItem(ACTIVE_JOB_KEY);
        return;
      }
      this.jobId = job.job_id;
      this.topic = job.topic;
      if (job.status === 'done') {
        // 任务已完成且综述已保存:不留活跃态
        sessionStorage.removeItem(ACTIVE_JOB_KEY);
        this.jobId = null;
        return;
      }
      if (job.status === 'failed') {
        this.stream = {
          ...initialState(),
          phase: 'error',
          error: job.error ?? '任务已失败',
          detail: job.error ?? '任务已失败(规划快照已保留,可确认主题后重试)',
        };
        sessionStorage.removeItem(ACTIVE_JOB_KEY);
        this.jobId = null;
        return;
      }
      // running / awaiting_confirm:重放事件恢复界面
      const snapshot = (job.req_snapshot ?? null) as WritingRequest | null;
      if (snapshot) this._planReq = snapshot;
      this._controller = markRaw(new AbortController());
      const runToken = this._runToken;
      const onUpdate = (s: StreamState) => {
        if (runToken !== this._runToken) return;
        this.stream = s;
      };
      this.running = job.status === 'running';
      try {
        const st = await consumeWritingJobStream({
          jobId,
          onUpdate,
          signal: this._controller?.signal,
        });
        if (st.phase === 'await_confirm') {
          this.awaitingConfirm = true;
          this.running = false;
        } else if (st.phase === 'complete') {
          // 恢复时恰好赶上完成:补保存(幂等,服务端权威副本调和覆盖)
          const planReq = this._planReq;
          if (planReq) {
            try {
              const finalResp = buildWritingResponse(st, planReq.topic, planReq.classify_mode, st.groups);
              await saveReview(finalResp);
            } catch {
              /* 门禁失败已在 st.error/detail 展示 */
            }
          }
          sessionStorage.removeItem(ACTIVE_JOB_KEY);
          this.jobId = null;
        }
      } catch (e: any) {
        if (e?.name === 'AbortError') return;
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

    /** 清除持久化的写作进度(用户主动点"清理"时使用) */
    clearProgress() {
      this.reset();
      this.topic = null;
    },
  },
});
