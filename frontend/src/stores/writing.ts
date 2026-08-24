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
import { generateWritingStream, type StreamState } from '@/api/streaming';
import type { WritingRequest } from '@/api/types';

const initialState: StreamState = {
  phase: 'idle',
  sections: [],
  groups: [],
  referenceList: '',
  screenedOutIds: [],
  droppedCitations: [],
  progress: null,
  currentSection: null,
  detail: null,
  error: null,
};

const SNAPSHOT_KEY = 'writing.snapshot.v1';

interface Snapshot {
  topic: string | null;
  stream: StreamState;
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

function saveSnapshot(topic: string, stream: StreamState) {
  // 只在「有产出」时持久化:避免每次 stream 变更都写
  const payload: Snapshot = {
    topic,
    stream: {
      ...stream,
      // currentSection.content 可能很长仍要存,便于刷新后展示已生成的章节
    },
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
    /** 当前写作的任务主题(用于切 tab 后展示) */
    topic: null as string | null,
    /** 当前写作请求的中止控制器(仅 start 期间有效) */
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
    },
    reset() {
      this.stream = { ...initialState };
      this.running = false;
      this._controller = null;
      clearSnapshot();
    },
    /** 启动写作。已在运行中则忽略,防止重复请求。 */
    async start(req: WritingRequest) {
      if (this.running) return;
      this.reset();
      this.running = true;
      this.topic = req.topic;
      this._controller = markRaw(new AbortController());
      const onUpdate = (s: StreamState) => {
        this.stream = s;
        // 阶段性产出时持久化,这样即使应用崩溃也能恢复
        if (s.sections.length > 0 || s.groups.length > 0) {
          saveSnapshot(req.topic, s);
        }
        // 写完后清理快照
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
      } catch (e: any) {
        // 用户主动停止,不当作错误
        if (e?.name === 'AbortError') {
          // 停止时保留当前进度,不清理
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
