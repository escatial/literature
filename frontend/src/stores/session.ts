/**
 * v7.0 任务隔离:全局 task_id session store。
 *
 * - 应用启动时自动生成 task_id(不调后端,本地生成时间戳格式 id)。
 * - 启动新检索时,UI 调用 newPaperSession(currentId),后端复用并清空该 task 的论文,
 *   返回的 task_id 写回本 store 与 localStorage。
 * - http.ts 的请求拦截器从 localStorage 读 X-Task-Id(同步源,避开循环依赖)。
 * - 整个生命周期内任意时刻只有一个 current task。
 */
import { defineStore } from 'pinia';
import { ref } from 'vue';
import { newPaperSession } from '@/api/endpoints';

/** 与 http.ts 共享同一个 LS_KEY,确保拦截器和 store 读同一份。 */
export const TASK_ID_LS_KEY = 'lit_review.current_task_id';

/**
 * v8.2:时间戳相关的任务 id —— `t-{yyyyMMddHHmmss}-{4位随机}`。
 * - 与时间戳相关:从 id 即可读出任务创建时刻,历史/日志肉眼可对账;
 * - 各任务 id 不同:秒级时间戳 + 随机后缀,同一秒内连点两次启动也不会撞。
 */
export function newTimestampId(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  const rand = Math.random().toString(36).slice(2, 6);
  return `t-${stamp}-${rand}`;
}

export const useSessionStore = defineStore('session', () => {
  /**
   * 当前任务 id(每次启动新检索时由后端 newPaperSession 刷新)。
   * 修复:X-Task-Id 断链 —— 初始生成的 uuid 必须立刻写回 localStorage。
   * 否则 http.ts 拦截器读 LS 为空 → 请求不带 X-Task-Id → 后端 papers 不打
   * task_id 标签(检索页从不调用 startNewTask,LS 永远不会被写入)。
   */
  const initialTaskId = localStorage.getItem(TASK_ID_LS_KEY) || newTimestampId();
  if (!localStorage.getItem(TASK_ID_LS_KEY)) {
    localStorage.setItem(TASK_ID_LS_KEY, initialTaskId);
  }
  const currentTaskId = ref<string>(initialTaskId);

  function setCurrent(id: string): void {
    currentTaskId.value = id;
    localStorage.setItem(TASK_ID_LS_KEY, id);
  }

  /**
   * 申请一个新 task。
   * 行为:
   *   - 后端复用 currentTaskId 时 → 清空该 task 的所有 papers,然后返回(reused=true)
   *   - 后端认为没传 header 时 → 生成新 UUID 返回(reused=false)
   * 返回后写回 store,后续所有 http 请求都会用新的 X-Task-Id。
   */
  async function startNewTask(): Promise<{ taskId: string; reused: boolean; cleared: number }> {
    const resp = await newPaperSession(currentTaskId.value);
    setCurrent(resp.task_id);
    return { taskId: resp.task_id, reused: resp.reused, cleared: resp.cleared };
  }

  /**
   * 本地生成全新 task_id(不依赖后端、无网络请求)。
   * v8 强隔离语义:每次「立即启动」检索都调用本方法,
   * 新旧任务在 papers / retrieval_history 等所有按 task 隔离的数据中互不可见。
   * 不再依赖「启动前清空旧池」——清空是时序脆弱的(请求乱序/多实例时会失效,导致跨任务混池)。
   */
  function newSession(): string {
    const id = newTimestampId();
    setCurrent(id);
    return id;
  }

  /** 短 task_id 显示(后 8 位)。 */
  const shortTaskId = () => currentTaskId.value.slice(-8);

  return { currentTaskId, shortTaskId, setCurrent, startNewTask, newSession };
});
