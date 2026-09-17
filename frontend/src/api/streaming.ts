/** SSE 流式写作客户端:逐章实时回填;两阶段模式(主题划分→用户确认→正文写作)。*/
import { getWritingStreamURL, getWritingPlanStreamURL, getWritingStopURL } from '@/config/api';
import type { Paper, WritingRequest, WritingResponse } from './types';

export type StreamPhase =
  | 'idle'
  | 'start'
  | 'screening'
  | 'classify'
  | 'await_confirm'
  | 'writing'
  | 'reference'
  | 'qa'
  | 'complete'
  | 'error';

export async function stopWritingRequest(): Promise<void> {
  await fetch(getWritingStopURL(), { method: 'POST' });
}

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
  blueprint?: ReviewBlueprint;
}

export interface ReviewBlueprint {
  research_question: string;
  scope: {
    time_range: [number | null, number | null];
    sources: string[];
    included_count: number;
    screened_out_count: number;
  };
  inclusion_criteria: string[];
  exclusion_criteria: string[];
  organization: {
    mode: string;
    principle: string;
    groups: {
      name: string;
      count: number;
      lit_ids: string[];
      shared_problem_terms?: string[];
      boundary?: string;
      comparison_axes?: string[];
      relation_hint?: string;
    }[];
  };
  matrix: {
    lit_id: string;
    title: string;
    authors: string[];
    year: number | null;
    source: string;
    research_question: string;
    theory_or_concept: string;
    method: string;
    data_or_sample: string;
    core_findings: string;
    limitation: string;
    relation: string;
    group: string;
  }[];
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
  /** QA 阶段进度(qa_started 置位,前端可展示「核查中」状态) */
  qaProgress: { checks: string[] } | null;
  /** QA 核查结果(qa_done 置位) */
  qaResult: {
    overall: string;
    pass_rate: number;
    checks: { check_id: string; name: string; status: string; fail_count: number; warn_count: number }[];
    issues: unknown[];
  } | null;
  detail: string | null;
  error: string | null;
  activityLog: { id: number; at: string; message: string }[];
}

// v9.6:改为工厂函数——此前模块级单例 + {...initialState} 浅拷贝,sections/
// droppedCitations 等数组仍是共享引用,section_done 就地 push 会污染单例,
// 第二次写作串入上一次章节、droppedCitations 跨会话累积并随 saveReview 持久化
function createInitialState(): StreamState {
  return {
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
    qaProgress: null,
    qaResult: null,
    detail: null,
    error: null,
    activityLog: [],
  };
}

/** 公共 SSE 消费循环:两个阶段共用同一套事件处理与心跳计时 */
async function consumeWritingSSE(
  url: string,
  req: WritingRequest,
  onUpdate: (s: StreamState) => void,
  signal?: AbortSignal,
): Promise<StreamState> {
  // v9.8:5xx 自动重试一次 —— 路由级 5xx 意味着服务端什么都没执行(未进流),
  // 重试幂等零成本;吸收开发态热重载/进程重启的瞬时窗口,避免用户看到
  // 凭空出现的"HTTP 500:服务器内部错误"还得手点重试
  const doFetch = () => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  });
  let resp = await doFetch();
  if (resp.status >= 500 && !signal?.aborted) {
    await new Promise((r) => setTimeout(r, 2000));
    resp = await doFetch();
  }

  if (!resp.ok || !resp.body) {
    // v9.7:读出响应体里的错误详情(后端全局异常处理器返回 {"detail": "..."}),
    // 此前只显示"HTTP 500",真实原因对用户完全不可见
    let detail = '';
    try {
      const text = await resp.text();
      try {
        detail = JSON.parse(text)?.detail ?? text;
      } catch {
        detail = text;
      }
    } catch {
      /* body 不可读时保持空 */
    }
    detail = String(detail).slice(0, 300);
    throw new Error(`HTTP ${resp.status}${detail ? `：${detail}` : ''}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let state: StreamState = createInitialState();
  const startedAt = Date.now();
  let lastProgressAt = startedAt;
  let activitySeq = 0;
  const addActivity = (message: string) => {
    const item = { id: ++activitySeq, at: new Date().toLocaleTimeString(), message };
    state.activityLog = [...state.activityLog, item].slice(-80);
  };
  const ticker = window.setInterval(() => {
    if (state.phase === 'complete' || state.phase === 'error') return;
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
            // 心跳也要推送到 Vue 状态；此前只更新了闭包变量，页面会一直显示 1 秒。
            state.waitingSeconds = Math.floor((Date.now() - lastProgressAt) / 1000);
            onUpdate({ ...state });
            break;
          case 'start':
            // 阶段2的 start 事件可能因代理缓冲晚于 writing_started 到达，
            // 不能把已经进入正文的状态回退成“初始化中”。
            if (!state.activityLog.some((item) => item.message.includes('主题已确认，开始生成综述正文'))) {
              state.phase = 'start';
              state.detail = `已接收 ${evt.data.total_papers ?? 0} 篇文献，准备启动综述写作...`;
              addActivity(state.detail ?? 'AI 质检正在运行');
            }
            break;
          case 'stopped':
            state.phase = 'idle';
            state.detail = evt.data.message ?? '已停止写作任务';
            state.error = null;
            onUpdate({ ...state });
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
            if (!state.activityLog.some((item) => item.message.includes('主题已确认，开始生成综述正文'))) {
              state.phase = 'classify';
            }
            state.detail = `正在按${evt.data.classify_mode === 'theme' ? '主题' : '国内外'}方式进行文献分组...`;
            addActivity(state.detail ?? 'AI 质检正在运行');
            break;
          case 'classify_progress': {
            // pipeline 内嵌 AI 质检 agent 的实时进度(感知查组/提交被拒/超时)
            state.phase = 'classify';
            const kind = String(evt.data.kind ?? '');
            if (kind === 'chunk_started') {
              state.detail = `正在处理第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块（${evt.data.count ?? 0} 篇）...`;
            } else if (kind === 'llm_attempt') {
              state.detail = `正在分析第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块...`;
            } else if (kind === 'llm_result') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.filter(Boolean) : [];
              const summary = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 块第 ${evt.data.attempt ?? 1} 次响应：解析 ${evt.data.parsed_groups ?? 0} 个主题，有效 ${evt.data.valid_groups ?? 0} 个，覆盖 ${evt.data.covered ?? 0}/${evt.data.papers ?? 0} 篇`;
              state.detail = summary;
              addActivity(`${summary}${themes.length ? `；主题：${themes.slice(0, 12).join('、')}${themes.length > 12 ? '等' : ''}` : '；未得到有效主题'}`);
            } else if (kind === 'chunk_done') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.filter(Boolean) : [];
              state.detail = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块完成：保留 ${evt.data.groups ?? 0} 个主题，覆盖 ${evt.data.covered ?? 0}/${evt.data.count ?? 0} 篇。`;
              addActivity(`${state.detail}${themes.length ? ` 主题：${themes.slice(0, 12).join('、')}${themes.length > 12 ? '等' : ''}` : ''}`);
            } else if (kind === 'fallback_result') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.filter(Boolean) : [];
              state.detail = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 块启用本地兜底：${evt.data.groups ?? 0} 个主题，覆盖 ${evt.data.covered ?? 0}/${evt.data.papers ?? 0} 篇。`;
              addActivity(`${state.detail}${themes.length ? ` 主题：${themes.join('、')}` : ''}`);
            } else if (kind === 'merge_candidates') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.slice(0, 6).join('、') : '';
              state.detail = evt.data.message ?? `已形成 ${evt.data.count ?? 0} 个候选主题，正在进行二级归并...`;
              if (themes) addActivity(`当前候选主题：${themes}${Number(evt.data.count) > 6 ? '等' : ''}`);
            } else if (kind === 'merge_done') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.join('、') : '';
              state.detail = `主题归并完成，共 ${evt.data.count ?? 0} 个主题。`;
              if (themes) addActivity(`归并后的主题：${themes}`);
            } else if (kind === 'local_cluster_started') {
              state.detail = evt.data.message ?? '正在基于标题与摘要进行本地语义聚类...';
            } else if (kind === 'local_cluster_done') {
              state.detail = evt.data.message ?? `本地摘要聚类完成，共形成 ${evt.data.count ?? 0} 个主题簇。`;
            } else if (kind === 'naming_started') {
              state.detail = evt.data.message ?? '主题簇已形成，正在生成可读主题名称...';
            } else if (kind === 'writing_pool_selected') {
              state.detail = evt.data.message ?? '已为各主题选择写作代表文献。';
              addActivity(state.detail ?? '已为各主题选择写作代表文献。');
            } else if (kind === 'inspect') {
              state.detail = `AI 质检：正在抽查分组《${evt.data.group}》（${evt.data.count ?? 0} 篇）...`;
            } else if (kind === 'submit_rejected') {
              state.detail = `AI 质检：模型提交被驳回（${evt.data.reason ?? '校验未通过'}），要求修正重提...`;
            } else if (kind === 'timeout') {
              state.detail = evt.data.message ?? 'AI 质检未在限定轮次内完成，保留初版分组。';
            } else {
              state.detail = evt.data.message ?? 'AI 质检：正在检查分组合理性...';
              addActivity(String(state.detail));
            }
            break;
          }
          case 'classify_done': {
            state.phase = evt.data.phase === 'writing' ? 'writing' : 'classify';
            state.groups = evt.data.groups ?? [];
            const base = `文献分组完成，共得到 ${state.groups.length} 个分组。`;
            if (state.groups.length) {
              const names = state.groups.map((g) => g.name).filter(Boolean).slice(0, 8).join('、');
              addActivity(`当前已形成主题：${names}${state.groups.length > 8 ? '等' : ''}`);
            }
            const agent = evt.data.agent as { checked?: boolean; changed?: boolean; note?: string } | null;
            state.detail = agent?.checked
              ? agent.changed
                ? `${base}AI 质检已修正分组${agent.note ? `：${agent.note}` : ''}。`
                : `${base}AI 质检认可当前分组。`
              : base;
            break;
          }
          case 'writing_started':
            state.phase = 'writing';
            state.detail = evt.data.message ?? '主题已确认，开始生成综述正文。';
            addActivity(String(state.detail));
            break;
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
              blueprint: evt.data.blueprint,
            };
            state.detail = state.plan.classify_fallback
              ? `模型主题划分未完全达标，已启用本地多主题兜底；请检查各主题后再开始写作。`
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
          case 'qa_started':
            // v9.6:此前无此分支,QA 阶段(可能持续数十秒)前端毫无进度反馈
            state.phase = 'qa';
            state.qaProgress = { checks: evt.data.checks ?? [] };
            state.detail = `正在执行全流程质量核查(${(evt.data.checks ?? []).length} 项)...`;
            break;
          case 'qa_done':
            state.qaResult = {
              overall: evt.data.overall ?? 'unknown',
              pass_rate: evt.data.pass_rate ?? 0,
              checks: evt.data.checks ?? [],
              issues: evt.data.issues ?? [],
            };
            state.detail = `质量核查完成:${evt.data.overall ?? 'unknown'}`;
            break;
          case 'qa_failed':
            // v9.7 软门禁:FAIL 只告警不终止流,详情在 qaResult(qa_done 已置位)
            state.detail = evt.data.message ?? '质量核查未通过';
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
  onUpdate(createInitialState());

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
  onUpdate(createInitialState());

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
