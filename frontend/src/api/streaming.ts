/** SSE 流式写作客户端:逐章实时回填;两阶段模式(主题划分→用户确认→正文写作)。
 *  C7:事件解析收敛到 api/sseTypes.parseSSELine(类型化判别联合),
 *  阶段判断以事件 phase 字段为准(替代界面文案匹配)。 */
import { getWritingJobStreamURL } from '@/config/api';
import { AUTH_TOKEN_KEY } from './auth';
import { friendlyLlmError } from '@/utils/llmErrors';
import { parseSSELine, phaseCanRegressTo } from './sseTypes';
import type { DeliveryManifest, Paper, QaReport, WritingGroup, WritingRequest, WritingResponse } from './types';

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

/** 写作池配额裁剪明细的一行(v11:相关性合格但超出写作池配额) */
export interface QuotaCutRow {
    lit_id: string;
    title: string;
    journal: string;
    year: number;
    language: 'zh' | 'en';
    core_tier: 'core' | 'non_core' | 'foreign';
    grade: string;
    total: number;
    reason: string;
}

/** 《文献相关性分级清单》完整结构 */
export interface RelevanceReport {
    summary: { high: number; medium: number; low: number };
    total: number;
    items: RelevanceRow[];
    excluded_low_relevance: RelevanceRow[];
    /** v11:配额精选裁剪明细(核心期刊分层 × 相关性分级 × 语言配额) */
    quota_cut?: QuotaCutRow[];
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
  /** v9.10: 分类策略透明化 —— degraded=true 表示 LLM 主题轴未交付,本次为本地快速聚类兜底 */
  classification?: { strategies: string[]; degraded: boolean };
  blueprint?: ReviewBlueprint;
}

export interface ReviewBlueprint {
  research_question: string;
  scope: {
    time_range: [number | null, number | null];
    sources: string[];
    included_count: number;
    screened_out_count: number;
    /** v11:配额精选裁剪数(超出写作池目标总量) */
    quota_cut_count?: number;
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

/** Agent 执行过程面板的一步(工具调用过程) */
export interface SectionStep {
  id: number;
  /** 机器名: prepare_context / llm_stream / citation_inject / humanize / qa_round … */
  step: string;
  label: string;
  status: 'running' | 'ok' | 'fail';
  at: string;
  seconds?: number;
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
  /** 当前章节的模型思考过程(reasoning 流式增量,仅展示不入正文) */
  currentReasoning: string;
  /** Agent 执行过程步骤(section_step 事件按序收集) */
  sectionSteps: SectionStep[];
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
  harness: {
    stage?: string;
    status?: string;
    attempt?: number;
    elapsed_seconds?: number;
    repairs?: unknown[];
    error?: string | null;
    trace?: unknown[];
  } | null;
  /** J1:阶段思考(通读/定轴的流式思考增量;stage 切换自动清空) */
  stageThinking: { label: string; text: string } | null;
  /** D1:摘要/关键词(abstract_done 事件) */
  abstract: string | null;
  keywords: string[] | null;
  /** D1:交付清单(complete 事件 manifest;保存前据此校验完整性) */
  manifest: DeliveryManifest | null;
  /** D1:最近一次 sections_finalized 是否到达(true = 正文已编号定稿) */
  finalized: boolean;
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
    currentReasoning: '',
    sectionSteps: [],
    qaProgress: null,
    qaResult: null,
    detail: null,
    error: null,
    activityLog: [],
    harness: null,
    stageThinking: null,
    abstract: null,
    keywords: null,
    manifest: null,
    finalized: false,
  };
}

/** SSE 读取循环(J1):消费一个已建立的 fetch Response,把事件按序灌入 state。
 * 连接只是「读取视图」;建流/重连/重放由 consumeWritingJobStream 编排。
 * 断言式收窄/文案拼接与旧实现逐行为保持一致(仅投递层改造)。 */
async function runSSELoop(
  resp: Response,
  state: StreamState,
  onUpdate: (s: StreamState) => void,
  onFrame?: () => void,
): Promise<void> {
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
  // D1:丢帧检测 —— 服务端为每个事件注入流内单调 seq,跳号即告警
  let lastSeq = 0;
  let lastProgressAt = Date.now();
  let activitySeq = 0;
  const addActivity = (message: string) => {
    const item = { id: ++activitySeq, at: new Date().toLocaleTimeString(), message };
    state.activityLog = [...state.activityLog, item].slice(-80);
  };
  let stepSeq = 0;
  /** section_step 事件:start 追加一行,ok/fail 就地收敛最近同名 running 行 */
  const applyStep = (data: any) => {
    const stepName = String(data.step ?? '');
    if (!stepName) return;
    const status: SectionStep['status'] =
      data.status === 'ok' ? 'ok' : data.status === 'fail' ? 'fail' : 'running';
    const message = String(data.message ?? stepName);
    const seconds = typeof data.seconds === 'number' ? data.seconds : undefined;
    const runningIdx = state.sectionSteps.findIndex(
      (s) => s.step === stepName && s.status === 'running',
    );
    if (status === 'running' || runningIdx < 0) {
      state.sectionSteps = [...state.sectionSteps, {
        id: ++stepSeq,
        step: stepName,
        label: message,
        status,
        at: new Date().toLocaleTimeString(),
        seconds,
      }].slice(-40);
    } else {
      const next = state.sectionSteps.slice();
      next[runningIdx] = { ...next[runningIdx], status, label: message, seconds };
      state.sectionSteps = next;
    }
    // 只把收敛结果写进活动日志,running 行不写(步骤面板已实时可见)
    if (status !== 'running') {
      addActivity(`${message}${seconds != null ? `(${seconds}s)` : ''}`);
    }
  };
  /** 每章开始:清空上一章的思考与步骤现场 */
  const resetSectionTrace = () => {
    state.currentReasoning = '';
    state.sectionSteps = [];
  };
  while (true) {
    let readResult: ReadableStreamReadResult<Uint8Array>;
    try {
      readResult = await reader.read();
    } catch (error) {
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
        // C7:单一解析入口(纯函数,类型化判别联合);坏帧静默跳过(与旧一致)
        const evt = parseSSELine(line);
        if (!evt) continue;
        if (typeof evt.seq === 'number' && evt.seq > 0) {
          if (lastSeq > 0 && evt.seq > lastSeq + 1) {
            console.warn(
              `[SSE] 检测到丢帧:期望 seq=${lastSeq + 1},收到 seq=${evt.seq}` +
              `(事件 ${evt.event})——交付完整性可能受损`,
            );
          }
          lastSeq = evt.seq;
        }

        if (evt.event !== 'heartbeat') {
          lastProgressAt = Date.now();
          onFrame?.();
          state.waitingSeconds = 0;
        }
        const evtData = evt.data as Record<string, unknown>;
        switch (evt.event) {
          // C7:agent_state 分支已删除 —— 后端已无 agent_state 产出方
          // (grep 零命中,契约清单见 api/sseTypes.ts),属死分支清理。
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
            // C7:以 phase 防回退替代界面文案匹配 —— 阶段2的 start 可能因
            // 代理缓冲晚于 writing_started 到达,不能把已进入正文/确认点的
            // 状态回退成“初始化中”。
            if (phaseCanRegressTo(state.phase, 'start')) {
              state.phase = 'start';
              state.detail = `已接收 ${evtData.total_papers ?? 0} 篇文献，准备启动综述写作...`;
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
            // 批次数不在前端估算(批大小由后端 _SCREENING_BATCH_SIZE 决定,
            // 前端硬编码会与后端漂移):初始置 0,等 screening_progress 事件
            // 携带后端真实 total_batches 后覆盖。
            state.screeningProgress = {
              batch: 0,
              totalBatches: 0,
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
            // C7:phase 防回退(此前按活动日志文案判断,后端改文案即坏)
            if (phaseCanRegressTo(state.phase, 'classify')) {
              state.phase = 'classify';
            }
            state.detail = `正在按${evtData.classify_mode === 'theme' ? '主题' : '国内外'}方式进行文献分组...`;
            addActivity(state.detail ?? 'AI 质检正在运行');
            break;
          case 'readthrough_started': {
            // W 批 R1 通读笔记进度:发生在 classify 期(定轴之前)。
            // 无 phase 字段,必须走防回退守卫 —— 高位阶段(writing 等)已
            // 进入时(代理缓冲导致事件迟到),不得把状态回退为 classify。
            if (phaseCanRegressTo(state.phase, 'classify')) {
              state.phase = 'classify';
            }
            state.detail = evt.data.message
              ?? `正在分块通读 ${evt.data.papers ?? 0} 篇文献并记录笔记...`;
            addActivity('正在通读文献,先读后划分主题...');
            break;
          }
          case 'readthrough_chunk_done': {
            if (phaseCanRegressTo(state.phase, 'classify')) {
              state.phase = 'classify';
            }
            state.detail = `正在通读文献(${evt.data.done ?? 0}/${evt.data.total ?? 0} 块)，已记录 ${evt.data.notes ?? 0} 条笔记...`;
            break;
          }
          case 'readthrough_done': {
            if (phaseCanRegressTo(state.phase, 'classify')) {
              state.phase = 'classify';
            }
            const notesCount = Number(evt.data.notes ?? 0);
            const papersCount = Number(evt.data.papers ?? 0);
            const failedChunks = Number(evt.data.failed_chunks ?? 0);
            state.detail = evt.data.message
              ?? `通读完成：${notesCount}/${papersCount} 篇已形成笔记，正在归纳主题轴...`;
            addActivity(
              `文献通读完成(${notesCount}/${papersCount} 篇笔记` +
              `${failedChunks > 0 ? `，${failedChunks} 块降级原文直通` : ''})，进入主题归纳`,
            );
            break;
          }
          case 'classify_progress': {
            // pipeline 内嵌 AI 质检 agent 的实时进度(感知查组/提交被拒/超时)
            state.phase = 'classify';
            const kind = String(evt.data.kind ?? '');
            if (kind === 'stage_thinking') {
              // J1:通读/定轴/筛选的思考或输出增量(后端节流合并,~0.8s/条);
              // stage 切换(R1→R2)自动清空重计,缓冲上限 24k 字符保护 DOM。
              // 注意:不推进 phase —— 筛选期的思考流不能把相位拉到 classify。
              const stage = String(evt.data.stage ?? 'classify');
              const label = stage === 'readthrough' ? '通读笔记'
                : stage === 'screening' ? '文献筛选' : '主题划分';
              const prev = state.stageThinking;
              const base = prev && prev.label === label ? prev.text : '';
              state.stageThinking = {
                label,
                text: (base + String(evt.data.delta ?? '')).slice(-24000),
              };
              break;
            }
            if (kind === 'chunk_started') {
              state.detail = `正在处理第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块（${evt.data.count ?? 0} 篇）...`;
            } else if (kind === 'synthesis_theme_started') {
              state.detail = String(evt.data.message ?? '正在综合主题...');
            } else if (kind === 'llm_attempt') {
              state.detail = `正在分析第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块...`;
            } else if (kind === 'llm_result') {
              const themes = Array.isArray(evt.data.themes) ? evt.data.themes.filter(Boolean) : [];
              const summary = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 块第 ${evt.data.attempt ?? 1} 次响应：解析 ${evt.data.parsed_groups ?? 0} 个主题，有效 ${evt.data.valid_groups ?? 0} 个，覆盖 ${evt.data.covered ?? 0}/${evt.data.papers ?? 0} 篇`;
              state.detail = summary;
              const decision = evt.data.decision === 'repair' ? '；检查未通过，进入自动修复' : '；检查通过';
              addActivity(`${summary}${decision}${themes.length ? `；主题：${themes.slice(0, 12).join('、')}${themes.length > 12 ? '等' : ''}` : '；未得到有效主题'}`);
            } else if (kind === 'llm_error') {
              const reason = `${evt.data.error_type ?? '模型错误'}：${evt.data.message ?? '无详细信息'}`;
              state.detail = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块第 ${evt.data.attempt ?? 1} 次调用失败：${reason}`;
              addActivity(state.detail ?? '模型输出格式不符合协议，正在自动修复...');
            } else if (kind === 'format_repair_started') {
              const repairMessage = evt.data.message ?? '模型输出格式不符合协议，正在自动修复...';
              state.detail = repairMessage;
              addActivity(repairMessage);
            } else if (kind === 'format_repair_done') {
              state.detail = `主题分类格式修复完成：解析 ${evt.data.groups ?? 0} 个主题，正在继续校验...`;
              addActivity(state.detail);
            } else if (kind === 'format_repair_failed') {
              state.detail = `主题分类格式修复失败：${evt.data.message ?? '未返回可用 JSON'}`;
              addActivity(state.detail);
            } else if (kind === 'chunk_failed') {
              state.detail = `第 ${evt.data.chunk ?? 0}/${evt.data.total_chunks ?? 0} 个文献块失败：${evt.data.message ?? '未形成有效主题'}`;
              addActivity(state.detail);
            } else if (kind === 'theme_validation_failed') {
              const issues = Array.isArray(evt.data.issues) ? evt.data.issues.join('；') : '';
              state.detail = `${evt.data.message ?? '主题方案校验失败'}${issues ? ` ${issues}` : ''}`;
              addActivity(state.detail);
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
            } else if (kind === 'still_alive') {
              // 心跳:长调用期间仅刷新等待提示,不写活动日志,避免每 5 秒刷屏。
              const wait = Number(evt.data.elapsed_since_call ?? 0);
              state.detail = `模型仍在返回中（本次调用已等待 ${Math.round(wait)} 秒）...`;
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
                        state.stageThinking = null;
            break;
          case 'plan_complete': {
            // 两阶段模式:阶段1到此为止,等用户确认主题划分后才进入正文写作
            state.phase = 'await_confirm';
            state.groups = evt.data.groups ?? state.groups;
            state.screenedOutIds = evt.data.screened_out_ids ?? state.screenedOutIds;
            const planPayload = {
              groups: evt.data.groups ?? [],
              section_titles: evt.data.section_titles ?? [],
              papers: evt.data.papers ?? [],
              screened_out_ids: evt.data.screened_out_ids ?? [],
              classify_fallback: evt.data.classify_fallback === true,
              classification: evt.data.classification,
              blueprint: evt.data.blueprint,
            };
            state.plan = planPayload;
            state.detail = planPayload.classify_fallback
              ? `模型主题划分未完全达标，已启用本地多主题兜底；请检查各主题后再开始写作。`
              : planPayload.classification?.degraded
                ? `模型主题轴方案未交付，本次为本地快速聚类兜底结果；主题名为标题证据命名，建议重试或手动调整主题名。`
                : `主题划分完成，共 ${planPayload.groups.length} 个主题，请确认或调整后开始写作。`;
            break;
          }
          case 'section_preparing':
            state.phase = 'writing';
            resetSectionTrace();
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
            resetSectionTrace();
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
          case 'section_notes_done': {
            // W 批 R1' 节级通读笔记完成:写作期内经带外通道发布,无 phase
            // 字段;对齐 section_step 展示模式(成功收敛为 ok 行,失败为
            // fail 行并写活动日志),失败的降级语义对用户如实可见。
            state.phase = 'writing';
            applyStep({
              step: 'section_notes',
              status: evt.data.notes === true ? 'ok' : 'fail',
              message: evt.data.notes === true
                ? `文献笔记完成（通读 ${evt.data.papers ?? 0} 篇）`
                : '文献笔记失败,改用既有证据层',
            });
            break;
          }
          case 'section_reasoning': {
            // 思考过程增量:仅上屏,绝不拼进正文 content
            state.phase = 'writing';
            if (state.currentSection && state.currentSection.key !== evt.data.key) {
              state.currentSection = {
                key: evt.data.key,
                title: evt.data.title,
                content: '',
              };
              resetSectionTrace();
            }
            state.currentReasoning = (
              state.currentReasoning + (evt.data.delta ?? '')
            ).slice(-12000);
            break;
          }
          case 'section_step': {
            state.phase = evt.data.step?.startsWith('qa_') ? 'qa' : state.phase;
            applyStep(evt.data);
            break;
          }
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
            // 正文锚点 [lit_xxx] 已替换为数字编号 [N],覆盖显示内容。
            // D1:QA 修复后会重发一次(final:true),以最后一次为准 ——
            // 此前 QA 修复结果从未送达前端(修了等于白修)。
            state.sections = evt.data.sections ?? state.sections;
            state.finalized = true;
            break;
          case 'delivery_warning':
            state.detail = String(evt.data.message ?? '交付不完整警告');
            break;
          case 'abstract_done':
            state.abstract = typeof evt.data.abstract === 'string' ? evt.data.abstract : null;
            state.keywords = Array.isArray(evt.data.keywords)
              ? evt.data.keywords.map(String)
              : null;
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
            state.manifest = (evt.data.manifest ?? null) as DeliveryManifest | null;
            if (evt.data.abstract && !state.abstract) {
              state.abstract = String(evt.data.abstract);
            }
            if (Array.isArray(evt.data.keywords) && !state.keywords) {
              state.keywords = evt.data.keywords.map(String);
            }
            state.detail = '综述生成完成。';
            break;
          case 'error':
            state.phase = 'error';
            // D2:服务商错误(402 额度不足等)友好化,用户直接看到该做什么
            state.error = friendlyLlmError(evt.data.message);
            state.detail = state.error;
            // 保留可重试的业务状态，页面可以明确提示用户重新划分，
            // 不把后端异常误显示成仍在运行。
            state.waitingSeconds = 0;
            break;
        }
        onUpdate({ ...state });
      }
    }
  }
}

// ============================================================================
// J1(2026-11) 任务化消费:任务事件流 + 断线重连 + 完整性门禁
// ============================================================================

/** 任务快照(GET /writing/jobs/{id})。 */
export interface WritingJobInfo {
  job_id: string;
  status: 'running' | 'awaiting_confirm' | 'done' | 'failed' | 'stopped';
  phase: string | null;
  topic: string;
  classify_mode: string;
  error: string | null;
  req_snapshot?: Record<string, unknown> | null;
}

export interface JobStreamOptions {
  jobId: string;
  onUpdate: (s: StreamState) => void;
  signal?: AbortSignal;
  /** 重放起点(默认 0 = 全量重放,经同一 reducer 重建全部界面状态) */
  sinceSeq?: number;
  /** 初始状态(默认全新;阶段2确认后可传当前 state 连续续写) */
  initialState?: StreamState;
}

const JOB_STREAM_MAX_CONSECUTIVE_FAILURES = 8;
const JOB_STREAM_RETRY_BASE_MS = 800;

/**
 * 任务事件流消费者(J1):GET /writing/jobs/{id}/stream。
 *
 * - 服务端先重放 since_seq 之后的事件,再实时 tail;连接只是读取视图;
 * - 连接在任务终态(done/failed/stopped/awaiting_confirm)由服务端收尾;
 * - 非终态提前断开(网络抖动/代理掐连接)= 退避重连,状态经重放无损重建;
 * - 返回时 phase ∈ complete | error | idle(stopped) | await_confirm。
 */
export async function consumeWritingJobStream(opts: JobStreamOptions): Promise<StreamState> {
  const state = opts.initialState ?? createInitialState();
  let lastSeq = opts.sinceSeq ?? 0;
  let lastFrameAt = Date.now();
  let consecutiveFailures = 0;
  const startedAt = Date.now();

  // 心跳/耗时计时:任何业务帧都重置 waiting;terminal 相位不再跳动
  const ticker = window.setInterval(() => {
    if (state.phase === 'complete' || state.phase === 'error' || state.phase === 'idle') return;
    state.elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
    state.waitingSeconds = Math.floor((Date.now() - lastFrameAt) / 1000);
    opts.onUpdate({ ...state });
  }, 1000);

  const onFrame = () => {
    lastFrameAt = Date.now();
  };

  const backendOrigin = (import.meta as any).env?.VITE_API_BASE as string | undefined;

  try {
    while (true) {
      if (opts.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      let resp: Response;
      try {
        resp = await fetch(getWritingJobStreamURL(backendOrigin, opts.jobId, lastSeq), {
          headers: { 'X-Auth-Token': localStorage.getItem(AUTH_TOKEN_KEY) ?? '' },
          signal: opts.signal,
        });
      } catch (e: any) {
        if (opts.signal?.aborted || e?.name === 'AbortError') throw e;
        consecutiveFailures += 1;
        if (consecutiveFailures > JOB_STREAM_MAX_CONSECUTIVE_FAILURES) {
          throw new Error('无法连接写作任务事件流(已重试多次),请检查后端服务');
        }
        await new Promise((r) => setTimeout(r, JOB_STREAM_RETRY_BASE_MS * consecutiveFailures));
        continue;
      }
      if (resp.status === 404) {
        throw new Error('写作任务不存在或无权访问(可能已被清理)');
      }
      if (!resp.ok || !resp.body) {
        consecutiveFailures += 1;
        if (consecutiveFailures > JOB_STREAM_MAX_CONSECUTIVE_FAILURES) {
          let detail = '';
          try {
            const text = await resp.text();
            detail = String(JSON.parse(text)?.detail ?? text).slice(0, 300);
          } catch {
            /* body 不可读时保持空 */
          }
          throw new Error(`HTTP ${resp.status}${detail ? `：${detail}` : ''}`);
        }
        await new Promise((r) => setTimeout(r, JOB_STREAM_RETRY_BASE_MS * consecutiveFailures));
        continue;
      }

      consecutiveFailures = 0;
      const seqBefore = lastSeq;
      await runSSELoop(resp, state, (s) => opts.onUpdate(s), () => {
        lastFrameAt = Date.now();
      });

      // 连接收尾后的判定:终态直接返回;非终态断开 → 退避重连(状态无损)
      if (
        state.phase === 'complete' || state.phase === 'error' ||
        state.phase === 'idle' || state.phase === 'await_confirm'
      ) {
        return state;
      }
      if (lastSeq === seqBefore) {
        // 一帧都没收到就被掐断:按网络故障退避
        consecutiveFailures += 1;
        if (consecutiveFailures > JOB_STREAM_MAX_CONSECUTIVE_FAILURES) {
          throw new Error('任务事件流反复中断且无新事件,请稍后刷新页面重新接入');
        }
        await new Promise((r) => setTimeout(r, JOB_STREAM_RETRY_BASE_MS * consecutiveFailures));
      } else {
        await new Promise((r) => setTimeout(r, 400));
      }
    }
  } finally {
    window.clearInterval(ticker);
  }
}

/**
 * D1 完成性门禁 + 响应构建(2026-11-01 交付事故修复):
 * 未收到 complete 或 manifest 校验不过,一律显式失败,绝不静默保存残品。
 */
export function buildWritingResponse(
  state: StreamState,
  topic: string,
  classifyMode: string,
  groups: WritingGroup[],
): WritingResponse {
  if (state.phase !== 'complete') {
    const done = state.sections.length;
    throw new Error(
      `生成流意外终止(未收到完成事件),已完成 ${done} 章未被保存。` +
      '请点击「开始生成」重试;已完成的主题划分不受影响。',
    );
  }
  const manifest = state.manifest;
  if (!manifest) {
    throw new Error('完成事件缺少交付清单(manifest),无法校验完整性,已拒绝保存。');
  }
  if (manifest.missing_sections?.length) {
    throw new Error(
      `交付不完整:以下章节生成失败未恢复(${manifest.missing_sections.join(', ')})。请重新生成。`,
    );
  }
  if (manifest.anchor_leaks > 0) {
    throw new Error(
      `交付异常:正文残留 ${manifest.anchor_leaks} 处未编号引用,已拒绝保存。请重新生成。`,
    );
  }
  if (state.sections.length !== manifest.sections.length) {
    throw new Error(
      `交付异常:实际收到 ${state.sections.length} 章,交付清单记录 ${manifest.sections.length} 章,已拒绝保存。`,
    );
  }
  return {
    topic,
    classify_mode: classifyMode,
    groups,
    sections: state.sections,
    reference_list: state.referenceList,
    screened_out_ids: state.screenedOutIds,
    dropped_citations: state.droppedCitations,
    qa_report: (state.qaResult ?? null) as WritingResponse['qa_report'],
    abstract: state.abstract,
    keywords: state.keywords,
  } satisfies WritingResponse;
}
