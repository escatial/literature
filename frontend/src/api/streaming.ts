/** SSE 流式写作客户端:逐章实时回填;两阶段模式(主题划分→用户确认→正文写作)。*/
import { getWritingStreamURL, getWritingPlanStreamURL } from '@/config/api';
import type { Paper, WritingRequest, WritingResponse } from './types';

export type StreamPhase =
  | 'idle'
  | 'start'
  | 'screening'
  | 'classify'
  | 'await_confirm'
  | 'writing'
  | 'reference'
  | 'complete'
  | 'error';

/** 《文献相关性分级清单》的一行 */
export interface RelevanceRow {
    lit_id: string;
    title: string;
    grade: 'high' | 'medium' | 'low';
    grade_label: string;
    total: number;
    match_points: string[];
    reason: string;
    dimensions: {
        keyword_overlap: number;
        field_match: number;
        method_applicability: number;
        conclusion_value: number;
    };
}

/** 《文献相关性分级清单》完整结构 */
export interface RelevanceReport {
    summary: { high: number; medium: number; low: number };
    total: number;
    items: RelevanceRow[];
    excluded_low_relevance: RelevanceRow[];
    criteria: {
        keyword_overlap_high: number;
        field_match_full: number;
        method_applicable: number;
        weights: Record<string, number>;
    };
}

/** plan_complete 事件载荷:阶段1产出,前端暂存,确认后随阶段2原样回传 */
export interface PlanPayload {
    groups: { name: string; lit_ids: string[] }[];
    section_titles: string[];
    papers: Paper[];
    screened_out_ids: string[];
    /** LLM 主题划分失败,单组兜底(前端应提示重新划分) */
    classify_fallback?: boolean;
}

export interface StreamState {
  phase: StreamPhase;
  relevanceReport: RelevanceReport | null;
  /** 阶段1产出的主题划分方案,确认点期间供用户编辑 */
  plan: PlanPayload | null;
  sections: { key: string; title: string; content: string; citations: string[] }[];
  groups: { name: string; lit_ids: string[] }[];
  referenceList: string;
  screenedOutIds: string[];
  droppedCitations: string[];
  progress: { index: number; total: number } | null;
  screeningProgress: {
    batch: number;
    totalBatches: number;
    processed: number;
    total: number;
    status: 'started' | 'completed';
  } | null;
  elapsedSeconds: number;
  waitingSeconds: number;
  currentSection: { key: string; title: string; content: string } | null;
  detail: string | null;
  error: string | null;
}

const initialState: StreamState = {
  phase: 'idle',
  relevanceReport: null,
  plan: null,
  sections: [],
  groups: [],
  referenceList: '',
  screenedOutIds: [],
  droppedCitations: [],
  progress: null,
  screeningProgress: null,
  elapsedSeconds: 0,
  waitingSeconds: 0,
  currentSection: null,
  detail: null,
  error: null,
};

/** 公共 SSE 消费循环:两个阶段共用同一套事件处理与心跳计时 */
async function consumeWritingSSE(
  url: string,
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
  signal?: AbortSignal,
): Promise<StreamState> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  });

  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let state: StreamState = { ...initialState };
  const startedAt = Date.now();
  let lastProgressAt = startedAt;
  const ticker = window.setInterval(() => {
    if (state.phase === 'complete' || state.phase === 'error' || state.phase === 'await_confirm') return;
    const now = Date.now();
    state = {
      ...state,
      elapsedSeconds: Math.floor((now - startedAt) / 1000),
      waitingSeconds: Math.floor((now - lastProgressAt) / 1000),
    };
    onUpdate({ ...state });
  }, 1000);

  while (true) {
    let readResult: ReadableStreamReadResult<Uint8Array>;
    try {
      readResult = await reader.read();
    } catch (error) {
      window.clearInterval(ticker);
      throw error;
    }
    const { value, done } = readResult;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split('\n\n');
    buffer = parts.pop() ?? '';

    for (const part of parts) {
      const lines = part.split('\n').filter(Boolean);
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        let evt: { event: string; data: any };
        try {
          evt = JSON.parse(payload);
        } catch {
          continue;
        }

        if (evt.event !== 'heartbeat') {
          lastProgressAt = Date.now();
          state.waitingSeconds = 0;
        }
        switch (evt.event) {
          case 'heartbeat':
            state.elapsedSeconds = Math.max(
              state.elapsedSeconds,
              Number(evt.data.elapsed_seconds ?? 0),
            );
            break;
          case 'start':
            state.phase = 'start';
            state.detail = `已接收 ${evt.data.total_papers ?? 0} 篇文献，准备启动综述写作...`;
            break;
          case 'screening_started':
            state.phase = 'screening';
            state.screeningProgress = {
              batch: 0,
              totalBatches: Math.ceil(Number(evt.data.total ?? 0) / 24),
              processed: 0,
              total: Number(evt.data.total ?? 0),
              status: 'started',
            };
            state.detail = evt.data.message
              ?? `筛选任务已启动，共 ${evt.data.total ?? 0} 篇候选文献，将并行准备最多 4 个批次...`;
            break;
          case 'screening_progress': {
            state.phase = 'screening';
            state.screeningProgress = {
              batch: evt.data.batch ?? 0,
              totalBatches: evt.data.total_batches ?? 0,
              processed: evt.data.processed ?? 0,
              total: evt.data.total ?? 0,
              status: evt.data.status === 'completed' ? 'completed' : 'started',
            };
            const p = state.screeningProgress;
            const parallelWorkers = Number(evt.data.parallel_workers ?? 0);
            state.detail = p.status === 'completed'
              ? `第 ${p.batch}/${p.totalBatches} 批筛选完成，已处理 ${p.processed}/${p.total} 篇，继续处理其余批次...`
              : `正在启动第 ${p.batch}/${p.totalBatches} 批${parallelWorkers > 1 ? `，同时并行 ${parallelWorkers} 批` : ''}；模型正在分析当前批次...`;
            break;
          }
          case 'screening_done':
            state.screenedOutIds = evt.data.screened_out ?? [];
            state.detail = `主题筛选完成，保留 ${evt.data.kept ?? 0} 篇，剔除 ${state.screenedOutIds.length} 篇。`;
            break;
          case 'relevance_report': {
            // 《文献相关性分级清单》：四维度打分 + 三级分级
            state.relevanceReport = evt.data as RelevanceReport;
            const s = state.relevanceReport.summary;
            state.detail = `相关性分级完成：高相关 ${s.high} 篇、中相关 ${s.medium} 篇、低相关 ${s.low} 篇。`;
            break;
          }
          case 'classify_started':
            state.phase = 'classify';
            state.detail = `正在按${evt.data.classify_mode === 'theme' ? '主题' : '国内外'}方式进行文献分组...`;
            break;
          case 'classify_progress': {
            // pipeline 内嵌 AI 质检 agent 的实时进度(感知查组/提交被拒/超时)
            state.phase = 'classify';
            const kind = String(evt.data.kind ?? '');
            if (kind === 'inspect') {
              state.detail = `AI 质检：正在抽查分组《${evt.data.group}》（${evt.data.count ?? 0} 篇）...`;
            } else if (kind === 'submit_rejected') {
              state.detail = `AI 质检：模型提交被驳回（${evt.data.reason ?? '校验未通过'}），要求修正重提...`;
            } else if (kind === 'timeout') {
              state.detail = evt.data.message ?? 'AI 质检未在限定轮次内完成，保留初版分组。';
            } else {
              state.detail = evt.data.message ?? 'AI 质检：正在检查分组合理性...';
            }
            break;
          }
          case 'classify_done': {
            state.phase = 'classify';
            state.groups = evt.data.groups ?? [];
            const base = `文献分组完成，共得到 ${state.groups.length} 个分组。`;
            const agent = evt.data.agent as { checked?: boolean; changed?: boolean; note?: string } | null;
            state.detail = agent?.checked
              ? agent.changed
                ? `${base}AI 质检已修正分组${agent.note ? `：${agent.note}` : ''}。`
                : `${base}AI 质检认可当前分组。`
              : base;
            break;
          }
          case 'plan_complete': {
            // 两阶段模式:阶段1到此为止,等用户确认主题划分后才进入正文写作
            state.phase = 'await_confirm';
            state.groups = evt.data.groups ?? state.groups;
            state.screenedOutIds = evt.data.screened_out_ids ?? state.screenedOutIds;
            state.plan = {
              groups: evt.data.groups ?? [],
              section_titles: evt.data.section_titles ?? [],
              papers: evt.data.papers ?? [],
              screened_out_ids: evt.data.screened_out_ids ?? [],
              classify_fallback: evt.data.classify_fallback === true,
            };
            state.detail = state.plan.classify_fallback
              ? `自动主题划分失败，当前为兜底单主题，建议点「重新划分」重试。`
              : `主题划分完成，共 ${state.plan.groups.length} 个主题，请确认或调整后开始写作。`;
            break;
          }
          case 'section_preparing':
            state.phase = 'writing';
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: '',
            };
            state.detail = evt.data.message ?? `正在准备《${evt.data.title}》...`;
            break;
          case 'section_started':
            state.phase = 'writing';
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: state.currentSection && state.currentSection.key === evt.data.key
                ? state.currentSection.content
                : '',
            };
            state.detail = `正在流式生成《${evt.data.title}》...`;
            break;
          case 'section_token': {
            state.phase = 'writing';
            const currentSection = (!state.currentSection || state.currentSection.key !== evt.data.key)
              ? {
                  key: evt.data.key,
                  title: evt.data.title,
                  content: '',
                }
              : state.currentSection;
            state.currentSection = {
              ...currentSection,
              content: `${currentSection.content}${evt.data.delta ?? ''}`,
            };
            const charCount = state.currentSection.content.length;
            state.detail = `正在流式生成《${state.currentSection.title}》，已输出 ${charCount} 字...`;
            break;
          }
          case 'section_done':
            state.sections.push({
              key: evt.data.key,
              title: evt.data.title,
              content: evt.data.content,
              citations: evt.data.citations ?? [],
            });
            state.droppedCitations.push(...(evt.data.dropped_citations ?? []));
            state.progress = { index: evt.data.index, total: evt.data.total };
            state.currentSection = {
              key: evt.data.key,
              title: evt.data.title,
              content: evt.data.content,
            };
            state.detail = `《${evt.data.title}》已完成，包含 ${(evt.data.citations ?? []).length} 处引用。`;
            break;
          case 'reference_started':
            state.phase = 'reference';
            state.detail = `正在整理参考文献，共 ${evt.data.count ?? 0} 篇...`;
            break;
          case 'sections_finalized':
            // 正文锚点 [lit_xxx] 已替换为数字编号 [N],覆盖显示内容
            state.sections = evt.data.sections ?? state.sections;
            break;
          case 'reference_list':
            state.phase = 'reference';
            state.referenceList = evt.data.reference_list ?? '';
            state.detail = '参考文献列表已生成，正在收尾...';
            break;
          case 'complete':
            state.phase = 'complete';
            state.detail = '综述生成完成。';
            break;
          case 'error':
            state.phase = 'error';
            state.error = evt.data.message;
            state.detail = evt.data.message;
            break;
        }
        onUpdate({ ...state });
      }
    }
  }
  window.clearInterval(ticker);
  return state;
}

/** 阶段1:主题划分(筛选+相关性分级+分类)。跑到确认点即停,不写正文。 */
export async function planWritingStream(
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
  signal?: AbortSignal,
): Promise<StreamState> {
  onUpdate(initialState);

  const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;
  const state = await consumeWritingSSE(getWritingPlanStreamURL(backendOrigin), req, onUpdate, signal);

  if (state.phase === 'error') {
    throw new Error(state.error ?? '主题划分失败');
  }
  if (state.phase !== 'await_confirm') {
    throw new Error('主题划分流意外终止,未到达确认点');
  }
  return state;
}

/** 阶段2:按用户确认的主题分组生成正文(confirmed_groups 模式,后端跳过筛选分类)。 */
export async function generateWritingStream(
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
  signal?: AbortSignal,
): Promise<WritingResponse> {
  onUpdate(initialState);

  const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;
  const state = await consumeWritingSSE(getWritingStreamURL(backendOrigin), req, onUpdate, signal);

  if (state.phase === 'error') {
    throw new Error(state.error ?? '流式生成失败');
  }
  return {
    topic: req.topic,
    classify_mode: req.classify_mode,
    groups: state.groups,
    sections: state.sections,
    reference_list: state.referenceList,
    screened_out_ids: state.screenedOutIds,
    dropped_citations: state.droppedCitations,
  } satisfies WritingResponse;
}
