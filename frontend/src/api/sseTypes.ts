/**
 * SSE 事件契约与类型化解析(C7,前后端单一事实来源)。
 *
 * ═══════════════ 事件名清单(前后端契约文档) ═══════════════
 * 来源:backend/src/writing/orchestrator.py 的 _sse_event(...) 调用
 * 与 backend/src/api/writing.py 的帧注入,2026-10-27 W 批盘点(共 33 个)。
 *
 * 流程编排类:
 *   start                流开始(C7 起携带 phase=planning)
 *   heartbeat            api/writing 注入的心跳(非 orchestrator)
 *   screening_started / screening_progress / screening_done
 *   relevance_report     《文献相关性分级清单》
 *   classify_started / classify_progress / classify_done
 *   readthrough_started / readthrough_chunk_done / readthrough_done
 *                        R1 通读笔记进度(W 批 additive;classify 期内,
 *                        经 classify 通道升格为独立事件名,无 phase 字段)
 *   plan_complete        阶段1终点,等确认(C7 起携带 phase=await_confirm)
 *   writing_started      阶段2正文开始(C7 起携带 phase=writing)
 *   synthesis_started / synthesis_done / synthesis_skipped
 * 章节粒度类:
 *   section_preparing / section_started / section_reasoning /
 *   section_step / section_token / section_done
 *   section_notes_done   R1' 节级通读笔记完成(W 批 additive,写作期内
 *                        经带外通道发布,无 phase 字段)
 * 交付与核查类:
 *   reference_started / sections_finalized / reference_list
 *   qa_started / qa_done / qa_failed
 * 终态类:
 *   complete(C7 起 phase=complete) / error / stopped(C7 起 phase=idle)
 *
 * 已退役事件(前端残留分支已于 C7 删除):agent_state(harness 面板,
 * 后端已无产出方)。
 *
 * C7 起:关键事件携带显式 phase 字段(planning/screening/classify/
 * await_confirm/writing/qa/complete/idle),前端状态判断一律以事件
 * 类型/phase 为准,禁止按界面文案匹配。W 批新增的 4 个通读事件不携带
 * phase,消费分支必须走 phaseCanRegressTo 守卫(通读发生在 classify 期,
 * 不得把 writing/await_confirm 等高位阶段回退为 classify)。
 *
 * 字段级载荷形状暂由消费分支自行收窄(与既有代码一致);本模块先锁定
 * 事件名枚举与解析收敛,字段级判别联合留作后续增量。
 */

/** 后端 orchestrator/api 实际会发出的全部事件名(运行时收窄用)。 */
export const SSE_EVENT_NAMES = [
  'start',
  'heartbeat',
  'screening_started',
  'screening_progress',
  'screening_done',
  'relevance_report',
  'classify_started',
  'classify_progress',
  'readthrough_started',
  'readthrough_chunk_done',
  'readthrough_done',
  'classify_done',
  'plan_complete',
  'writing_started',
  'synthesis_started',
  'synthesis_done',
  'synthesis_skipped',
  'section_preparing',
  'section_started',
  'section_notes_done',
  'section_reasoning',
  'section_step',
  'section_token',
  'section_done',
  'reference_started',
  'sections_finalized',
  'reference_list',
  'qa_started',
  'qa_done',
  'qa_failed',
  'complete',
  'error',
  'stopped',
] as const;

export type SseEventName = (typeof SSE_EVENT_NAMES)[number];

/** 事件可能携带的显式阶段标记(C7 additive)。 */
export type SsePhase =
  | 'planning'
  | 'screening'
  | 'classify'
  | 'await_confirm'
  | 'writing'
  | 'qa'
  | 'reference'
  | 'complete'
  | 'idle';

/** 事件 phase → 前端 StreamPhase 的显式回退序:C7 用以替代文案匹配 ——
 *  高位阶段已进入时,低位迟到事件不得把状态回退(如代理缓冲导致
 *  start 晚于 writing_started 到达)。数值大者不可被小者覆盖。 */
const PHASE_RANK: Record<string, number> = {
  idle: 0,
  start: 1,
  screening: 2,
  classify: 3,
  await_confirm: 4,
  writing: 6,
  reference: 7,
  qa: 7,
  complete: 9,
  error: 9,
};

/** 判断是否允许把当前 phase 回退为 target(用于 start/classify_started 这类
 *  低位事件的防回退守卫)。 */
export function phaseCanRegressTo(current: string, target: string): boolean {
  return (PHASE_RANK[target] ?? 0) >= (PHASE_RANK[current] ?? 0);
}

/** 类型化事件帧:event 收窄到枚举,data 由消费分支自行收窄。 */
export interface SseEventFrame {
  event: SseEventName;
  data: Record<string, any>;
}

/**
 * 单行 SSE data 帧解析(纯函数)。
 * 输入形如 `data: {"event":"...","data":{...}}`;非 data 行/空载荷/坏 JSON/
 * 未知事件名一律返回 null(与旧解析行为一致)。
 */
export function parseSSELine(line: string): SseEventFrame | null {
  if (!line.startsWith('data:')) return null;
  const payload = line.slice(5).trim();
  if (!payload) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }
  if (
    typeof parsed !== 'object' || parsed === null ||
    !('event' in parsed) || !('data' in parsed)
  ) {
    return null;
  }
  const evt = parsed as { event: string; data: unknown };
  if (!(SSE_EVENT_NAMES as readonly string[]).includes(evt.event)) {
    return null;
  }
  return {
    event: evt.event as SseEventName,
    data: (typeof evt.data === 'object' && evt.data !== null ? evt.data : {}) as Record<string, any>,
  };
}
