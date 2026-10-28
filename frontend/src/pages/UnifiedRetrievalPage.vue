<script lang="ts">
// v9.6:SSE 订阅注册表必须放模块级——放 <script setup> 会随组件实例重建,
// 「启动检索 → 切走再切回」后 Map 清空导致同一 task_id 重复订阅;后端每任务
// 单队列,消息随机分流给两个 EventSource,收不到 done 的连接被服务端关闭后
// 触发 onerror,任务被误标失败
const sseSources = new Map<string, EventSource>();
</script>
<script setup lang="ts">
/** v4.0 统一检索页
 *
 * - 主题输入 → 生成检索式 → 启动任务
 * - 中文:仅知网,走后端 /api/cnki/start(headless 全自动)+ EventSource 订阅 SSE 进度
 * - 英文:PubMed / OpenAlex HTTP 任务,每 2s 轮询一次进度
 * - 不再使用远程浏览器画布,验证码由超级鹰后台自动接管
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessageBox } from 'element-plus';
import { toast } from '@/utils/toast';
import { usePapersStore } from '@/stores/papers';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';
import { useLLMProvidersStore } from '@/stores/llmProviders';
import { useAuthStore } from '@/stores/auth';
import { http, apiBaseURL } from '@/api/http';
import { useSessionStore, newTimestampId } from '@/stores/session';
import {
  queryPlan,
  createRetrievalTask,
  getRetrievalTask,
  cnkiStreamUrl,
  startCnkiFullAuto,
  stopRetrieval,
  listRetrievalHistory,
  restoreRetrievalHistory,
  deleteRetrievalHistory,
  listPapers,
  testLLM,
} from '@/api/endpoints';
import type { RetrievalTask } from '@/api/types';
import type { RetrievalHistory } from '@/api/endpoints';

const router = useRouter();
const REQUIRED_DATABASES = ['cnki', 'openalex', 'pubmed'] as const;
const REQUIRED_DATABASE_LABELS = {
  cnki: '中国知网',
  openalex: 'OpenAlex',
  pubmed: 'PubMed',
} as const;
// v7.1:展示 LLM provider 配置状态(推荐/备用/配置保留)
const llmProviders = useLLMProvidersStore();
const testingLLM = ref(false);
const testLLMConnection = async () => {
  if (testingLLM.value) return;
  testingLLM.value = true;
  try {
    const result = await testLLM();
    toast.success(`LLM 连接成功：${result.provider} / ${result.model}，${result.elapsed_ms}ms，响应：${result.response_preview || '有响应'}`);
    await llmProviders.refresh();
  } catch (e: any) {
    toast.error(`LLM 连接失败：${e?.message ?? e}`);
    await llmProviders.refresh().catch(() => undefined);
  } finally {
    testingLLM.value = false;
  }
};

// ---------- v9.11: MiniMax 模型选择(从上游获取 + 用户自选) ----------
// 2026-10-27 起用户感知:已配置自定义 LLM 的用户,模型探测/切换作用于
// 自己的配置;未配置用户给出提醒并引导到「监控与管理 → LLM 设置」。
interface MiniMaxModel { id: string; owned_by: string; active: boolean }
const auth = useAuthStore();
const availableModels = ref<MiniMaxModel[]>([]);
const selectedModel = ref("");
const currentModel = ref("");
const modelsLoading = ref(false);
const selectingModel = ref(false);
/** 响应里的上游失败详情(用户上游不可达时展示) */
const upstreamError = ref('');

const userLlmConfigured = computed(() =>
  !!(auth.user?.llm_base_url && auth.user?.llm_api_key_masked));
const userLlmKindLabel = computed(() =>
  auth.user?.llm_kind === 'anthropic' ? 'Anthropic 兼容' : 'OpenAI 兼容');
const userLlmBaseHost = computed(() => {
  try {
    return new URL(auth.user?.llm_base_url || '').host;
  } catch {
    return auth.user?.llm_base_url || '';
  }
});

const goLlmSettings = () => {
  router.push({ path: '/crawler', query: { tab: 'llm' } });
};

const fetchUpstreamModels = async () => {
  modelsLoading.value = true;
  upstreamError.value = '';
  try {
    const resp = await http.get("/review/models/upstream");
    availableModels.value = resp.data.models ?? [];
    currentModel.value = resp.data.active ?? "";
    upstreamError.value = String(resp.data.error ?? '');
    if (upstreamError.value) {
      toast.error(upstreamError.value);
    }
    if (!selectedModel.value && availableModels.value.length) {
      selectedModel.value = currentModel.value || availableModels.value[0]?.id || "";
    }
  } catch (e: any) {
    toast.error(`获取模型列表失败: ${e?.message ?? e}`);
  } finally {
    modelsLoading.value = false;
  }
};

const applyModel = async () => {
  if (!selectedModel.value) return;
  selectingModel.value = true;
  try {
    const resp = await http.post("/review/models/select", { model_id: selectedModel.value });
    currentModel.value = selectedModel.value;
    // 用户级切换:同步本地会话里的配置,状态栏即时反映
    if (resp.data?.scope === 'user' && auth.user) {
      auth.user.llm_model = selectedModel.value;
    }
    toast.success(`模型已切换为 ${selectedModel.value}`);
    availableModels.value.forEach((m: MiniMaxModel) => { m.active = m.id === selectedModel.value; });
  } catch (e: any) {
    toast.error(`切换失败: ${e?.message ?? e}`);
  } finally {
    selectingModel.value = false;
  }
};

// 页面加载时:已配置用户自动获取一次自己上游的模型列表
onMounted(() => {
  if (auth.user === null) {
    void auth.restore().then(() => {
      currentModel.value = auth.user?.llm_model || '';
      if (userLlmConfigured.value) void fetchUpstreamModels();
    });
  } else {
    currentModel.value = auth.user.llm_model || '';
    if (userLlmConfigured.value) void fetchUpstreamModels();
  }
});

const runStarted = ref(false);
const runFailed = ref(false);
const runFailureMessage = ref('');
let autoRetryTimer: number | null = null;

// 英文任务进度
const englishTask = ref<RetrievalTask | null>(null);
let pollTimer: number | null = null;
// v9.6:轮询连续失败计数(单次失败不清任务引用,见 refreshProgress)
let pollFailStreak = 0;

// 需求4:历史检索(最多 5 条)
const history = ref<RetrievalHistory[]>([]);
const historyLoadError = ref('');

const refreshHistory = async () => {
  try {
    historyLoadError.value = '';
    history.value = await listRetrievalHistory(5);
  } catch (e: any) {
    historyLoadError.value = `历史记录加载失败：${String(e?.message ?? e)}`;
  }
};

const viewHistory = async (row: RetrievalHistory) => {
  try {
    await ElMessageBox.confirm(
      `将用该条历史的 ${row.total_count} 篇文献覆盖当前文献池,并跳转到文献池。是否继续?`,
      '查看历史检索',
      { type: 'warning' },
    );
    const resp = await restoreRetrievalHistory(row.id);
    ustore.setTopic(row.topic);
    toast.success(`已加载历史文献 ${resp.total} 篇`);
    router.push('/pool');
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') {
      toast.error(`查看失败:${String(e?.message ?? e)}`);
    }
  }
};

const deletingId = ref<number | null>(null);
const removeHistory = async (row: RetrievalHistory) => {
  if (deletingId.value !== null) return;
  deletingId.value = row.id;
  try {
    await ElMessageBox.confirm(
      `确定删除检索记录「${row.topic}」?其数据库中的文献快照将一并删除,不可恢复。`,
      '删除历史',
      { type: 'warning' },
    );
    await deleteRetrievalHistory(row.id);
    toast.success('已删除');
    await refreshHistory();
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') {
      toast.error(`删除失败:${String(e?.message ?? e)}`);
    }
  } finally {
    deletingId.value = null;
  }
};

const formatTime = (iso: string) => {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return iso;
  }
};

// 中文 v4.0 任务进度(以 db 为 key,持久化到 store,切 tab 不丢)
const cnkiTasks = computed((): typeof ustore.cnkiTasks => {
  const task = ustore.cnkiTasks.cnki;
  return task ? { cnki: task } : {};
});
// (sseSources 已移到模块级 <script> 块,见文件头——组件重建后去重仍生效)

const ustore = useUnifiedRetrievalStore();
const papersStore = usePapersStore();
const sessionStore = useSessionStore();
const topicInput = computed({
  get: () => ustore.topic,
  set: (value: string) => {
    ustore.setTopic(value);
  },
});

const isRunning = computed(() =>
  Boolean(
    Object.values(cnkiTasks.value).some(
      (t) => t.stage !== 'done' && t.stage !== 'error' && t.stage !== undefined,
    ) ||
    (englishTask.value &&
      !['succeeded', 'failed'].includes(englishTask.value.status || '')),
  ),
);

/** 本次任务实际入库数(中英合并,含失败的源)。
 *
 * 关键:必须等于本次任务各库进度条 saved 之和,与下方进度条完全对齐。
 *  - 中文:cnkiTasks.cnki.saved(知网 SSE 推送的真实入库数)
 *  - 英文:优先用后端权威 total_after_filter(Controller 池内按 lit_id 去重后的
 *    入库数);任务未完成 / 未取到该字段时退回 openalex.saved + pubmed.saved。
 *
 * 注意:绝不能用 listPapers 的文献池全库 total 覆盖——那是跨任务累积的,
 * 会混入历史任务的文献,导致「本次 154 篇、汇总 414 篇」的异常。
 */
const totalRetrieved = computed(() => {
  const cnki = Object.values(cnkiTasks.value).reduce((acc, t) => acc + (t.saved ?? 0), 0);
  const enAuthoritative = englishTask.value?.total_after_filter ?? 0;
  const en = enAuthoritative > 0
    ? enAuthoritative
    : (ustore.enTasks.openalex.saved ?? 0) + (ustore.enTasks.pubmed.saved ?? 0);
  return cnki + en;
});
/** 需求1:成功导入文献池的文献数——与文献池页面严格同口径。
 *  修复「显示打架」:saved 口径(lit_id 去重的检索侧统计)与池内实际行数
 *  (identity_key 二次去重 + provenance 校验淘汰)存在差值,之前 importedSuccess
 *  直接等于 totalRetrieved,导致汇总「成功导入 300」而文献池只有 276。
 *  现在任务完成后从池 API 取实际 total(拦截器自动带 X-Task-Id),取不到再回退 saved。 */
const poolActualTotal = ref<number | null>(null);
const importedSuccess = computed(() => poolActualTotal.value ?? totalRetrieved.value);
// v7.2:补上缺失的取数实现——此前 poolActualTotal 只有「重置为 null」,
// 从未真正赋值,importedSuccess 永远回退 saved 口径(如 590),而池子实际 569,
// 「检索总数 vs 成功导入 vs 文献池」三个数字永远对不上。
// 任务全部结束(isRunning true→false)时从池 API 取当前任务实际 total,
// 拦截器自动带 X-Task-Id,与文献池页面严格同口径。
watch(isRunning, async (running, prev) => {
  if (prev && !running && totalRetrieved.value > 0) {
    try {
      const resp = await listPapers({ page: 1, page_size: 1 });
      poolActualTotal.value = resp.total;
    } catch {
      /* 取失败则保持回退 saved 口径 */
    }
  }
});
/** 需求1:异常条目——任务失败或部分源失败时填这里,作为告警明细。 */
const failureEntries = computed<Array<{ source: string; message: string }>>(() => {
  const out: Array<{ source: string; message: string }> = [];
  if (englishTask.value?.status === 'failed') {
    out.push({
      source: '英文检索',
      message: englishTask.value.error || '英文任务失败',
    });
  } else if (englishTask.value) {
    // 部分源失败明细
    const events = englishTask.value.events || [];
    const sources = new Set<string>();
    events.forEach((e) => {
      if (e.source && (e.stage === 'filling_warning' ||
        (e.stage === 'fetching_source' && /翻页失败|放弃/.test(e.message || '')))) {
        if (!sources.has(e.source)) {
          sources.add(e.source);
          out.push({ source: e.source, message: e.message || '检索异常' });
        }
      }
    });
  }
  Object.values(cnkiTasks.value).forEach((t) => {
    if (t.stage === 'error') {
      out.push({ source: '中国知网', message: t.msg || t.stage || '知网任务失败' });
    }
  });
  return out;
});
const hasFailureEntries = computed(() => failureEntries.value.length > 0);

const tasksFinished = computed(() => {
  const cnkiDone = REQUIRED_DATABASES.includes('cnki') &&
    (ustore.cnkiTasks.cnki?.stage === 'done' || ustore.cnkiTasks.cnki?.stage === 'error');
  const enStatus = englishTask.value?.status;
  const enDone = Boolean(englishTask.value) && ['succeeded', 'failed'].includes(enStatus || '');
  return runFailed.value || (cnkiDone && enDone);
});

/** 检索完成时(英文 succeeded + 中英都 done)主动刷新文献池 tab,
 *  避免用户切到「文献池」tab 时还是 0 条 / 旧数据。
 *  注意:不在这里覆盖汇总数——文献池是全库累积,汇总只看本次任务(saved)。 */
let lastRefreshedTaskId: string | null = null;
watch(tasksFinished, (finished: boolean) => {
  if (!finished) return;
  const t = englishTask.value;
  if (t?.status !== 'succeeded') return; // 只在真正入库后才拉
  if (lastRefreshedTaskId === t.task_id) return;
  lastRefreshedTaskId = t.task_id;
  papersStore.refresh({ page: 1 }).catch(() => { /* 已弹错 */ });
});

let lastHistoryRefreshKey: string | null = null;
watch(tasksFinished, (finished: boolean) => {
  if (!finished) return;
  const key = [
    englishTask.value?.task_id || '',
    cnkiTasks.value.cnki?.task_id || '',
  ].filter(Boolean).join(':');
  if (!key || lastHistoryRefreshKey === key) return;
  lastHistoryRefreshKey = key;
  void refreshHistory();
  window.setTimeout(() => void refreshHistory(), 1000);
  window.setTimeout(() => void refreshHistory(), 3000);
});

const hasNoResults = computed(() => tasksFinished.value && !hasTaskFailure.value && totalRetrieved.value === 0);
const hasTaskFailure = computed(
  () =>
    runFailed.value ||
    REQUIRED_DATABASES.some((db) => db === 'cnki'
      ? ustore.cnkiTasks.cnki?.stage === 'error'
      : englishTask.value?.status === 'failed'),
);
/** 部分源失败但任务整体成功(如 OpenAlex SSL 闪断)——给一个警告栏而不是失败 alert */
const hasTaskWarning = computed(() => {
  if (!englishTask.value) return false;
  if (englishTask.value.status !== 'succeeded') return false;
  const warns = (englishTask.value.events || []).some(
    (e) => e.stage === 'filling_warning' || e.stage === 'fetching_source' && /翻页失败|放弃/.test(e.message || ''),
  );
  return warns;
});
/** 当前任务精确错误信息(失败 / 警告 共用) */
const taskErrorMessage = computed(() => {
  if (runFailureMessage.value) return runFailureMessage.value;
  if (englishTask.value?.status === 'failed') {
    return englishTask.value.error || 'OpenAlex / PubMed 检索任务失败';
  }
  const cnkiErr = Object.values(cnkiTasks.value).find((t) => t.stage === 'error');
  if (cnkiErr) {
    return cnkiErr.msg || cnkiErr.stage || '中国知网检索任务失败';
  }
  return '';
});
const taskWarningMessage = computed(() => {
  if (!englishTask.value) return '';
  const events = englishTask.value.events || [];
  const warnEvents = events.filter(
    (e) => e.stage === 'filling_warning' || (e.stage === 'fetching_source' && /翻页失败|放弃/.test(e.message || '')),
  );
  if (!warnEvents.length) return '';
  // 取最近 3 条;过长截断
  return warnEvents.slice(-3).map((e) => `${e.source ? `[${e.source}] ` : ''}${e.message}`).join('；');
});

/** 订阅单个知网任务的 SSE。task_id 已订阅则跳过。
 *  收到 done/error 自动 close;不做过度的重连/兜底——爬虫任务是单向流。
 */
const subscribeCnki = (db: string, initial: typeof cnkiTasks.value[string]) => {
  if (sseSources.has(initial.task_id)) return;
  const es = new EventSource(cnkiStreamUrl(initial.task_id));
  sseSources.set(initial.task_id, es);
  es.addEventListener('cnki_progress', (e) => {
    try {
      const msg = JSON.parse((e as MessageEvent).data);
      // 过程日志只追加,不覆盖任务阶段
      if (msg.stage === 'log') {
        if (msg.msg) ustore.appendCnkiLog(db, msg.msg);
        return;
      }
      // 真有事件进来(plan/search/fetched/done/error…)时,把 stage
      // 推回 active(避免前一轮 onerror 把 task 标 error 后,新一轮
      // SSE 起来时仍一直显示「失败」)。
      // v9.6:merge 基于 store 当前值而非订阅时刻的 initial 快照——
      // 后端不带 saved 的事件(尤其 error)此前会把已入库计数清零显示
      const cur = ustore.cnkiTasks[db];
      const merged = { ...(cur || initial), ...msg };
      // v9.8:标记进度所处阶段 —— list_progress(检索条目累计)与
      // fetched(逐篇入库)分属进度条的两段区间,切换时不回跳
      if (msg.stage === 'list_progress') merged.listPhase = true;
      if (msg.stage === 'fetched') merged.listPhase = false;
      // v9.8:长等待倒计时 —— waiting 事件带秒数,其余事件意味着等待已结束
      if (msg.stage === 'waiting') {
        merged.waitUntil = Date.now() + (msg.wait_seconds ?? 0) * 1000;
        merged.waitReason = msg.reason || '等待';
      } else {
        merged.waitUntil = undefined;
      }
      if (msg.stage === 'search_done' && msg.ok === false) {
        // 产品级:0 篇入库必须标为失败,否则任务停在 active →
        // 进度条显示「0 篇 已完成」误导用户;文案不暴露内部机制
        merged.stage = 'error';
        merged.msg = '未获取到文献：数据源暂时不可用（可能受访问限制），请稍后重试，或适当调低目标篇数';
        ustore.appendCnkiLog(db, `[失败] ${merged.msg}`);
      } else if (
        merged.stage &&
        merged.stage !== 'done' &&
        merged.stage !== 'error'
      ) {
        merged.stage = 'active';
      }
      ustore.upsertCnkiTask(db, merged);
      if (merged.stage === 'done' || merged.stage === 'error') {
        es.close();
        sseSources.delete(initial.task_id);
      }
    } catch {
      /* noop */
    }
  });
  // v9.6:onerror 不再无条件标失败——
  // 1) readyState=CONNECTING 表示浏览器正在自动重连(如代理 idle 断流),
  //    判死会让仍在跑的任务被误标;只有 CLOSED 才判失败;
  // 2) 任务已到终态(done/error)时不覆盖。
  es.onerror = () => {
    const prev = ustore.cnkiTasks[db];
    if (prev && (prev.stage === 'done' || prev.stage === 'error')) {
      es.close();
      sseSources.delete(initial.task_id);
      return;
    }
    if (es.readyState === EventSource.CONNECTING) {
      return; // 等浏览器自动重连,不断流不判死
    }
    runFailed.value = true;
    runFailureMessage.value = '中国知网检索连接中断，本次三库任务失败，请重新开始';
    ustore.appendCnkiLog(db, `[错误] ${runFailureMessage.value}`);
    ustore.upsertCnkiTask(db, {
      ...(prev || initial),
      stage: 'error',
      msg: runFailureMessage.value,
    });
    es.close();
    sseSources.delete(initial.task_id);
  };
};

// ─────────────── 三库进度条(统一结构) ───────────────
// 每库一条:进度百分比 + 状态 + 已入库数 + 最新一条日志。
// 进度优先级: 终态→100; 有界任务(progress_total>0)→ done/total; 否则→ saved/目标。

// v9.8:秒级心跳 —— 长等待倒计时靠它每秒刷新;静止进度条在用户眼里与坏了无异
const nowMs = ref(Date.now());
let waitTicker: number | undefined;

/** 三库进度栏统一数据结构 */
interface ProgressBar {
  key: string;
  name: string;
  tag: 'danger' | 'success' | 'primary';
  percent: number;
  status: 'exception' | 'success' | undefined;
  saved: number;
  target: number;
  running: boolean;
  lastLog: string;
  /** 处于长等待(退避/冷却):进度条切流光动画,配实时倒计时 */
  waiting: boolean;
  waitRemaining: number;
  waitReason: string;
}
const cnkiProgress = computed<ProgressBar>(() => {
  const task = cnkiTasks.value.cnki;
  const target = ustore.autoTarget || 100;
  // 未启动:返回占位对象,进度栏始终渲染,日志显示「等待开始…」
  if (!task) {
    return {
      key: 'cnki',
      name: '中国知网',
      tag: 'danger',
      percent: 0,
      status: undefined,
      saved: 0,
      target,
      running: false,
      lastLog: '',
      waiting: false,
      waitRemaining: 0,
      waitReason: '',
    };
  }
  let percent = 0;
  if (task.stage === 'done' || task.stage === 'error') percent = 100;
  else if ((task.progress_total ?? 0) > 0) {
    const total = task.progress_total ?? 0;
    const ratio = Math.min(1, (task.progress_done ?? 0) / total);
    // v9.8 分段映射:列表(检索条目累计)占 0-49%,详情(逐篇入库)占 50-99%
    // —— 此前两阶段共用同一刻度,进入详情时进度条从 60% 跳回 3% 像卡死
    percent = task.listPhase
      ? Math.round(ratio * 49)
      : Math.min(99, 50 + Math.round(ratio * 49));
  } else {
    percent = Math.min(49, Math.round(((task.saved ?? 0) / target) * 49));
  }
  const logs = task.logs ?? [];
  const running = task.stage !== 'done' && task.stage !== 'error';
  const waitUntil = task.waitUntil ?? 0;
  const waiting = running && waitUntil > nowMs.value;
  return {
    key: 'cnki',
    name: '中国知网',
    tag: 'danger',
    percent,
    status: task.stage === 'error' ? ('exception' as const) : task.stage === 'done' ? ('success' as const) : undefined,
    saved: task.saved ?? 0,
    target,
    running,
    lastLog: logs[logs.length - 1] ?? '',
    waiting,
    waitRemaining: waiting ? Math.ceil((waitUntil - nowMs.value) / 1000) : 0,
    waitReason: task.waitReason ?? '',
    _stage: task.stage,
  };
});

const enProgressFor = (db: 'openalex' | 'pubmed') =>
  computed<ProgressBar>(() => {
    const row = ustore.enTasks[db];
    const target = ustore.autoTarget || 100;
    const enStatus = englishTask.value?.status;
    // 是否已被纳入本次任务(勾选或已启动)
    const started = Boolean(row?.task_id) || (row?.logs?.length ?? 0) > 0;
    const finished = enStatus === 'succeeded' || enStatus === 'failed';
    let percent = 0;
    if (enStatus === 'failed') percent = 100;
    else if (enStatus === 'succeeded') percent = 100;
    else percent = Math.min(99, Math.round(((row?.saved ?? 0) / target) * 100));
    const logs = row?.logs ?? [];
    const def =
      db === 'openalex'
        ? { name: 'OpenAlex', tag: 'success' as const }
        : { name: 'PubMed', tag: 'primary' as const };
    return {
      key: db,
      name: def.name,
      tag: def.tag,
      percent,
      status: enStatus === 'failed' ? ('exception' as const) : enStatus === 'succeeded' ? ('success' as const) : undefined,
      saved: row?.saved ?? 0,
      target,
      running: started && !finished,
      lastLog: logs[logs.length - 1] ?? '',
      waiting: false,
      waitRemaining: 0,
      waitReason: '',
    };
  });

const openalexProgress = enProgressFor('openalex');
const pubmedProgress = enProgressFor('pubmed');

/** 三库进度栏统一结构:知网 + OpenAlex + PubMed 各一条,始终渲染(未启动显示等待) */
const progressBars = computed<ProgressBar[]>(() => [
  cnkiProgress.value,
  openalexProgress.value,
  pubmedProgress.value,
]);

// 三库进度栏始终渲染(未启动显示「待开始 / 等待开始…」),无需按激活状态隐藏

// ─────────────── 进度轮询(英文) ───────────────
const stopProgressPolling = () => {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
};
const startProgressPolling = () => {
  stopProgressPolling();
  pollTimer = window.setInterval(refreshProgress, 2000);
};
const refreshProgress = async () => {
  if (englishTask.value?.task_id) {
    try {
      const t = await getRetrievalTask(englishTask.value.task_id);
      pollFailStreak = 0;
      englishTask.value = t;
      // v4.1:把后端事件同步到 store,按 db 拆分 OpenAlex / PubMed 过程日志
      if (t.events && t.events.length) {
        ustore.ingestEnEvents(t.task_id, t.events);
      }
    } catch {
      // v9.6:单次轮询失败不清任务引用——置 null 后本函数再也不发请求,
      // 但下方停止条件永远不满足(僵尸轮询 2s 空转),停止按钮也随引用丢失失效。
      // 改记连续失败次数,连续 5 次(约 10s)才在控制台告警;引用保留可继续停止。
      pollFailStreak += 1;
      if (pollFailStreak === 5) {
        console.warn('[检索] 任务状态轮询连续失败,网络可能不稳定,仍在重试…');
      }
    }
  }
  // 知网 SSE 自然结束,无需轮询
  const allCnkiClosed = Object.values(cnkiTasks.value).every(
    (t) => t.stage === 'done' || t.stage === 'error',
  );
  if (allCnkiClosed && (!englishTask.value || !['succeeded', 'failed'].includes(englishTask.value.status))) {
    return;
  }
  if (allCnkiClosed && (!englishTask.value || ['succeeded', 'failed'].includes(englishTask.value.status))) {
    stopProgressPolling();
  }
};

// ─────────────── 检索式展示区(第一栏) ───────────────
const planPreview = computed(() => [
  { key: 'cnki', name: REQUIRED_DATABASE_LABELS.cnki, tag: 'danger' as const, query: ustore.queriesCnki[0] || '', queries: ustore.queriesCnki },
  { key: 'openalex', name: REQUIRED_DATABASE_LABELS.openalex, tag: 'success' as const, query: ustore.queriesOpenalex[0] || '', queries: ustore.queriesOpenalex },
  { key: 'pubmed', name: REQUIRED_DATABASE_LABELS.pubmed, tag: 'primary' as const, query: ustore.queriesPubmed[0] || '', queries: ustore.queriesPubmed },
]);

// ─────────────── 检索式生成 ───────────────
const generatePlan = async () => {
  const topic = ustore.topic.trim();
  if (!topic) return false;

  ustore.setPlanning(true);
  try {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const resp = await queryPlan(topic);
        ustore.applyPlan({
          topic_summary: resp.topic_summary || '',
          queries_cnki: resp.queries_cnki || [],
          queries_openalex: resp.queries_openalex || [],
          queries_pubmed: resp.queries_pubmed || [],
        });
        return true;
      } catch (e) {
        lastError = e;
        if (attempt < 2) {
          ustore.appendCnkiLog('cnki', `[编排] 检索式生成失败，${attempt + 1} 秒后自动重试…`);
          await new Promise((resolve) => window.setTimeout(resolve, (attempt + 1) * 1000));
        }
      }
    }
    throw lastError ?? new Error('检索式生成失败');
  } catch (e) {
    const msg = (e as Error)?.message ?? String(e);
    ustore.appendCnkiLog('cnki', `[错误] 检索式自动生成失败：${msg}`);
    // 显眼弹窗(不再只追加 log)。常见原因:LLM provider 不通 / 返回不合法 JSON / 超时。
    toast.error(`检索式生成失败:${msg}`);
    return false;
  } finally {
    ustore.setPlanning(false);
  }
};

const scheduleAutoRetry = (topic: string) => {
  if (autoRetryTimer !== null) return;
  // 最多自动重试 3 次(避免 LLM 持续不通时无限循环、用户看不到尽头)
  // 每次重试间隔 5 秒;超过上限后彻底停手,要求用户手动「重新启动」。
  const maxAutoRetries = 3;
  ustore.autoRetryCount = (ustore.autoRetryCount || 0) + 1;
  if (ustore.autoRetryCount > maxAutoRetries) {
    runFailed.value = true;
    runFailureMessage.value =
      `已自动重试 ${maxAutoRetries} 次仍失败,请检查后端 LLM provider 配置或网络,然后点击「立即启动」重试。`;
    toast.error(runFailureMessage.value);
    return;
  }
  autoRetryTimer = window.setTimeout(async () => {
    autoRetryTimer = null;
    if (ustore.topic.trim() !== topic) return;
    if (isRunning.value) await stopAll();
    runFailed.value = false;
    runFailureMessage.value = '';
    void startUnifiedRetrieval();
  }, 5000);
};

/** 用户手动点「立即启动」时清空重试计数。 */
const resetAutoRetryCount = () => {
  ustore.autoRetryCount = 0;
};

// ─────────────── 一键全自动 ───────────────
const startUnifiedRetrieval = async () => {
  const topic = ustore.topic.trim();
  if (!topic) {
    toast.warning('请输入研究主题');
    return;
  }
  runStarted.value = true;
  runFailed.value = false;
  runFailureMessage.value = '';
  // ★ 任务完全隔离(v8):每次启动生成全新 task_id,新旧任务互不可见。
  //   旧池数据留在旧 task 名下(检索历史可查),绝不混入本次结果。
  //   不再依赖「启动前清空」——清空在请求乱序/多实例场景下会失效,导致跨任务混池。
  sessionStore.newSession();
  // 用户主动重试 → 清空「自动重试计数」,允许下一轮失败后再次自动重试 3 次。
  resetAutoRetryCount();
  // 每次点击都从三库全量重启，不能沿用上次只选部分数据源的旧状态。
  ustore.setDbs([...REQUIRED_DATABASES]);
  ustore.clearCnkiTasks();
  ustore.initEnTasks('');
  englishTask.value = null;

  const cnkiSelected = true;
  const englishSelected = true;
  ustore.initCnkiTask('cnki', topic);
  ustore.appendCnkiLog('cnki', '[检索式] 正在根据主题生成中英文概念组和知网专业检索式…');

  const planReady = await generatePlan();
  if (!planReady || !ustore.queriesCnki.length || !ustore.queriesOpenalex.length || !ustore.queriesPubmed.length) {
    ustore.appendCnkiLog('cnki', '[编排] 三库检索式未完整生成，系统将自动重新规划，不需要用户操作');
    scheduleAutoRetry(topic);
    return;
  }
  if (cnkiSelected) {
    ustore.appendCnkiLog('cnki', `[检索式] 已生成 ${ustore.queriesCnki.length} 条候选式，将按顺序预检`);
    ustore.queriesCnki.forEach((query, index) => {
      ustore.appendCnkiLog('cnki', `[检索式 ${index + 1}] ${query}`);
    });
    ustore.appendCnkiLog('cnki', '[本地] 正在将专业检索式提交到 /api/cnki/start …');
  }

  try {
    // v8 任务隔离:池按 task 天然隔离,无需启动前清空(旧池留在旧任务名下,互不可见)
    poolActualTotal.value = null;

    toast.info('启动自动检索(知网 v4.0 + 英文 PubMed/OpenAlex)…');

    // 本次「启动自动检索」的 runId:中文 + 英文两边共享,后端 aggregator 用它合并写一条历史。
    // v8.2:时间戳相关 id(t-yyyyMMddHHmmss-xxxx),各任务不同、肉眼可对账。
    const runId = newTimestampId();
    ustore.setRunId(runId);

    const startCnkiTask = async () => {
      if (!cnkiSelected) return;
      try {
        const resp = await startCnkiFullAuto({
          topic: ustore.topic,
          expert_query: ustore.queriesCnki[0] || '',
          expert_queries: ustore.queriesCnki,
          target_count: ustore.autoTarget,
          max_pages: ustore.autoMaxPages,
          db_type: 'cnki',
          run_id: runId,
        });
        ustore.appendCnkiLog('cnki', `[本地] 后端已分配任务 ${resp.task_id.slice(0, 8)}…,正在建立 SSE 订阅`);
        const initial = { task_id: resp.task_id, db_type: 'cnki' as const, stage: 'starting' };
        ustore.upsertCnkiTask('cnki', initial);
        subscribeCnki('cnki', initial);
      } catch (e: any) {
        const msg = String(e?.message ?? e);
        runFailed.value = true;
        runFailureMessage.value = `中国知网未能启动，系统将在 5 秒后自动重试：${msg}`;
        ustore.appendCnkiLog('cnki', `[错误] 调用 /api/cnki/start 失败: ${msg}`);
        ustore.upsertCnkiTask('cnki', {
          task_id: '', db_type: 'cnki' as const, stage: 'error', msg: runFailureMessage.value,
        });
        scheduleAutoRetry(topic);
      }
    };

    const startEnglishTask = async () => {
      if (!englishSelected) return;
      try {
        const resp = await createRetrievalTask({
          topic: ustore.topic,
          min_citations: 0,
          limit: ustore.autoTarget,
          use_rerank: false,
          use_snowball: false, // 默认不开启雪球(引文回溯),避免大量无关引文混入池
          sources: ['openalex', 'pubmed'],
          run_id: runId,
        });
        ustore.englishTaskId = resp.task_id || '';
        ustore.initEnTasks(resp.task_id);
        const enDbs = ['openalex', 'pubmed'];
        for (const d of enDbs) {
          ustore.appendEnLog(d as 'openalex' | 'pubmed', '[本地] 已点击「启动自动检索」,等待后端响应…');
        }
        const t = await getRetrievalTask(resp.task_id);
        englishTask.value = t;
        if (t.events && t.events.length) {
          ustore.ingestEnEvents(t.task_id, t.events);
        }
      } catch (e: any) {
        const msg = String(e?.message ?? e);
        runFailed.value = true;
        runFailureMessage.value = `OpenAlex / PubMed 未能同时启动，系统将在 5 秒后自动重试：${msg}`;
        toast.warning(runFailureMessage.value);
        scheduleAutoRetry(topic);
      }
    };

    await Promise.allSettled([startCnkiTask(), startEnglishTask()]);
    startProgressPolling();
  } catch (e: any) {
    const msg = String(e?.message ?? e);
    ustore.appendCnkiLog('cnki', `[错误] 三库任务启动异常，系统将在 5 秒后自动重试: ${msg}`);
    scheduleAutoRetry(topic);
  }
};

// ─────────────── 一键停止(知网 + 英文全停) ───────────────
const stopping = ref(false);

const stopAll = async () => {
  if (stopping.value) return;
  stopping.value = true;
  try {
    // 收集当前运行中的任务 id(知网 + 英文各一个)
    const ids: string[] = [];
    const cnkiRow = cnkiTasks.value.cnki;
    if (cnkiRow?.task_id) ids.push(cnkiRow.task_id);
    if (englishTask.value?.task_id) ids.push(englishTask.value.task_id);
    // 后端置位取消标志,线程循环尽快退出
    await stopRetrieval(ids.length ? ids : undefined);
    // 本地立即清理:关 SSE、停轮询、标为"已手动停止"
    sseSources.forEach((es) => es.close());
    sseSources.clear();
    stopProgressPolling();
    if (cnkiRow) {
      ustore.appendCnkiLog('cnki', '[停止] 用户已手动停止,正在终止知网爬虫…');
      ustore.upsertCnkiTask('cnki', {
        task_id: cnkiRow.task_id, db_type: 'cnki' as const,
        stage: 'error', msg: '用户已手动停止',
      });
    }
    if (englishTask.value) {
      englishTask.value = { ...englishTask.value, status: 'failed', error: '用户已手动停止' };
    }
    toast.success('已发送停止指令,正在终止所有检索任务');
  } catch (e: any) {
    toast.error(`停止失败: ${String(e?.message ?? e)}`);
  } finally {
    stopping.value = false;
  }
};

onMounted(async () => {
  ustore.reset();
  sessionStore.newSession();
  runStarted.value = false;
  // v9.8:秒级心跳驱动长等待倒计时(等待中进度条为流光动画+实时秒数)
  waitTicker = window.setInterval(() => { nowMs.value = Date.now(); }, 1000);
  // v7.1:加载 LLM provider 状态(后台异步,不阻塞主题输入)
  llmProviders.refresh().catch(() => { /* 接口挂了也不挡用户 */ });
  await refreshHistory();
});

onBeforeUnmount(() => {
  sseSources.forEach((es) => es.close());
  sseSources.clear();
  ustore.reset();
  englishTask.value = null;
  runStarted.value = false;
  stopProgressPolling();
  if (waitTicker !== undefined) {
    window.clearInterval(waitTicker);
    waitTicker = undefined;
  }
  // SSE 不立刻关闭,后台仍继续推消息;事件触发 onprogress 会再写 store
  // 切换路由回来时 onMounted 会重连,避免双重订阅靠 task_id 去重
});
</script>

<template>
  <!-- 壹 · 选题与启动 -->
  <el-card shadow="never" class="sheet">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">壹</span>
          <span class="section-title">选题与启动</span>
        </div>
        <span class="section-note">一次提交,三库并发:知网 · OpenAlex · PubMed</span>
      </div>
    </template>

    <div class="topic-row">
      <span class="topic-label">研究主题</span>
      <el-input
        v-model="topicInput"
        placeholder="写下你要综述的问题…"
        size="large"
        class="topic-input"
        clearable
        :disabled="isRunning"
        @keyup.enter="startUnifiedRetrieval"
      />
      <el-button
        type="primary"
        size="large"
        class="launch-btn"
        :loading="ustore.planning"
        :disabled="isRunning || !topicInput.trim()"
        @click="startUnifiedRetrieval"
      >
        {{ '立即启动' }}
      </el-button>
      <el-button size="large" plain :loading="stopping" :disabled="!isRunning" @click="stopAll">
        停止
      </el-button>
    </div>

    <div v-if="ustore.planning || isRunning" class="run-banner">
      <span class="run-dot" :class="{ wait: ustore.planning }"></span>
      <span class="run-text">{{ ustore.planning ? 'Agent 正在自动生成三库检索式…' : 'Agent 正在自动检索,可切到其他页继续工作' }}</span>
    </div>

    <!-- LLM 状态条(用户感知):显示当前用户自己的接口配置;未配置则提醒 -->
    <div class="llm-providers-bar">
      <span class="llm-label">誊抄</span>
      <template v-if="userLlmConfigured">
        <el-tag type="success" effect="dark" size="small">
          {{ userLlmKindLabel }}<span>·我的</span>
        </el-tag>
        <span class="llm-current">{{ userLlmBaseHost }}</span>
        <el-button
          size="small"
          text
          bg
          :loading="testingLLM"
          @click="testLLMConnection"
        >
          测试 LLM
        </el-button>
        <span class="llm-divider" aria-hidden="true"></span>
        <span class="llm-label">模型</span>
        <el-select
          v-model="selectedModel"
          placeholder="选择模型"
          size="small"
          style="width: 280px"
          :loading="modelsLoading"
          :disabled="selectingModel"
          clearable
          filterable
        >
          <el-option
            v-for="m in availableModels"
            :key="m.id"
            :label="m.id + (m.active ? ' (当前)' : '')"
            :value="m.id"
          />
        </el-select>
        <el-button size="small" text bg :loading="modelsLoading" @click="fetchUpstreamModels">
          从上游获取
        </el-button>
        <el-button
          size="small"
          type="primary"
          plain
          :disabled="!selectedModel || selectedModel === currentModel || selectingModel"
          :loading="selectingModel"
          @click="applyModel"
        >
          切换模型
        </el-button>
        <span v-if="currentModel" class="llm-current">当前 {{ currentModel }}</span>
      </template>
      <template v-else>
        <el-tag type="warning" effect="plain" size="small">未配置自定义 LLM</el-tag>
        <span class="llm-remind">
          你还没有配置自己的大模型接口,当前使用系统默认 MiniMax(模型 {{ currentModel || '未指定' }})。
          建议配置你自己的 Base URL / API Key,写作任务将使用你的接口与额度。
        </span>
        <el-button size="small" type="warning" plain @click="goLlmSettings">
          去配置
        </el-button>
        <el-button
          size="small"
          text
          bg
          :loading="testingLLM"
          @click="testLLMConnection"
        >
          测试 LLM
        </el-button>
      </template>
    </div>
  </el-card>

  <!-- 贰 · 检索式 -->
  <el-card shadow="never" class="sheet" style="margin-top: 18px">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">贰</span>
          <span class="section-title">检索式</span>
        </div>
        <span class="section-note">实际按拆分式逐条遍历检索,结果合并去重;零结果时自动放宽</span>
      </div>
    </template>
    <div class="query-grid">
      <div v-for="item in planPreview" :key="item.key" class="query-card" :class="item.key === 'cnki' ? 'is-cn' : 'is-en'">
        <div class="query-card-head">
          <span class="query-db">{{ item.name }}</span>
          <span class="query-alt" v-if="item.key === 'cnki'">中文库</span>
          <span class="query-alt" v-else>英文库</span>
        </div>
        <div v-if="item.queries.length > 1" class="query-strategy">
          展示第 1 条实际执行式 · 共 {{ item.queries.length }} 条候选
        </div>
        <pre class="query-code">{{ item.query || '—— 待生成 ——' }}</pre>
        <!-- 英文长检索式按语义单元拆分的子检索式(依次执行、合并去重) -->
        <el-collapse v-if="item.queries.length > 1" class="query-sub">
          <el-collapse-item
            :title="`查看 ${item.queries.length} 条实际执行式`"
            name="sub-queries"
          >
            <pre
              v-for="(q, qi) in item.queries"
              :key="qi"
              class="query-code query-code-sub"
            >{{ qi + 1 }}. {{ q }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>
  </el-card>

  <!-- 叁 · 三库进度(统一结构,始终渲染) -->
  <el-card shadow="never" class="sheet" style="margin-top: 18px">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">叁</span>
          <span class="section-title">三库进度</span>
        </div>
      </div>
    </template>
    <div class="prog-list">
      <div v-for="bar in progressBars" :key="bar.key" class="prog-row" :class="bar.key === 'cnki' ? 'is-cn' : 'is-en'">
        <div class="prog-head">
          <span class="prog-name">{{ bar.name }}</span>
          <span class="prog-count"><b class="figure-num">{{ bar.saved }}</b> / {{ bar.target }} 篇</span>
          <el-tooltip
            v-if="bar.key !== 'cnki'"
            content="该数字为该源单源累加入池数;三库累加 ≠ 「检索总数量」是预期的跨源去重效应(同 lit_id 跨多源 / 跨子检索式只算一次)"
            placement="top"
          >
            <span class="prog-help">ⓘ</span>
          </el-tooltip>
          <span class="prog-state" :class="`st-${bar.status === 'exception' ? 'bad' : bar.status === 'success' ? 'ok' : bar.waiting ? 'wait' : bar.running ? 'run' : 'idle'}`">
            {{ bar.status === 'exception' ? '失败' : bar.status === 'success' ? '已完成' : bar.waiting ? '等待中' : bar.running ? '进行中' : '待开始' }}
          </span>
        </div>
        <el-progress
          :percentage="bar.waiting ? 100 : bar.percent"
          :status="bar.status"
          :stroke-width="8"
          :indeterminate="bar.waiting"
          :duration="2"
        />
        <div class="prog-log">
          <span class="prog-log-label">最新日志</span>
          <span class="prog-log-text">
            {{ bar.waiting
              ? `${bar.waitReason || '等待'}中，约 ${bar.waitRemaining}s 后自动继续…`
              : (bar.lastLog || '等待开始…') }}
          </span>
        </div>
      </div>
    </div>
  </el-card>

  <!-- 肆 · 结果与记录 -->
  <el-card v-if="hasNoResults" shadow="never" class="sheet" style="margin-top: 18px">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">肆</span>
          <span class="section-title">本次收稿</span>
        </div>
      </div>
    </template>
    <el-empty description="未检索到有效文献,可调整主题或放宽检索式" :image-size="70" />
  </el-card>
  <el-card v-else-if="tasksFinished" shadow="never" class="sheet" style="margin-top: 18px">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">肆</span>
          <span class="section-title">本次收稿</span>
          <span
            class="state-chip"
            :class="hasTaskFailure ? 'st-bad' : hasFailureEntries ? 'st-warn' : 'st-ok'"
          >
            {{ hasTaskFailure ? '任务失败' : hasFailureEntries ? '部分异常' : '已完成' }}
          </span>
        </div>
      </div>
    </template>
    <!-- v7.2:用户只关心最后入库多少——只展示「入库文献」一个数字,
         与文献池页面严格同口径(任务完成后取池 API 实际 total)。
         检索总数/拦截数等中间口径不再展示,差异全部沉淀在质量闸门内部。 -->
    <div class="harvest">
      <div class="harvest-num figure-num">{{ importedSuccess }}</div>
      <div class="harvest-meta">
        <div class="harvest-label">入库文献</div>
        <div class="harvest-sub">与文献池同口径 · 任务完成后按池内实际条数取值</div>
      </div>
    </div>
    <el-alert v-if="hasTaskFailure" type="error" :closable="false" show-icon style="margin-top: 14px" :title="taskErrorMessage || '任务失败'" />
    <el-alert v-else-if="hasTaskWarning" type="warning" :closable="false" show-icon style="margin-top: 14px" :title="`部分源异常,已自动忽略:${taskWarningMessage}`" />
  </el-card>

  <el-card shadow="never" class="sheet" style="margin-top: 18px">
    <template #header>
      <div class="section-head">
        <div class="head-main">
          <span class="section-no">附</span>
          <span class="section-title">检索记录</span>
          <span class="section-note-inline">{{ history.length }} 条</span>
        </div>
        <el-button size="small" text bg @click="refreshHistory">刷新</el-button>
      </div>
    </template>
    <el-alert
      v-if="historyLoadError"
      type="error"
      :closable="false"
      show-icon
      :title="historyLoadError"
    />
    <el-empty
      v-else-if="!history.length"
      description="暂无已完成的三库检索记录"
      :image-size="70"
    />
    <el-table v-else :data="history" stripe size="small">
      <el-table-column label="检索时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="检索主题" min-width="240" prop="topic" />
      <el-table-column label="文献数" width="100" prop="total_count" />
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button size="small" type="primary" link @click="viewHistory(row)">查看</el-button>
          <el-button
            size="small"
            type="danger"
            link
            :loading="deletingId === row.id"
            :disabled="deletingId !== null"
            @click="removeHistory(row)"
          >删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

</template>

<style scoped>
.sheet {
  /* 卡片间距由模板 margin 控制 */
}

/* ── 壹 · 选题行 ── */
.topic-row {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.topic-label {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: 0.14em;
  flex: none;
}
.topic-input {
  flex: 1;
  min-width: 300px;
}
.launch-btn {
  letter-spacing: 0.3em;
  padding-left: calc(0.3em * 2);
}

/* 运行横幅:纸上的墨线状态条 */
.run-banner {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: 12px;
  padding: 8px 12px;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--cinnabar);
  background: var(--sheet-dim);
  font-size: 12.5px;
  color: var(--ink-soft);
}
.run-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cinnabar);
  animation: pulse 1.4s ease-in-out infinite;
}
.run-dot.wait {
  background: var(--ink-ghost);
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
.run-text {
  letter-spacing: 0.02em;
}

/* ── LLM 状态条 ── */
.llm-providers-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--rule-soft);
}
.llm-label {
  font-family: var(--font-annotate);
  font-size: 12.5px;
  color: var(--ink-faint);
  letter-spacing: 0.2em;
}
.llm-divider {
  width: 1px;
  height: 16px;
  background: var(--rule-strong);
  margin: 0 2px;
}
.llm-providers-hint {
  font-size: 13px;
  color: var(--ink-ghost);
  cursor: help;
}
.llm-current {
  font-size: 12px;
  color: var(--ink-faint);
  font-family: var(--font-mono);
}
.llm-remind {
  font-size: 12px;
  color: var(--ink-soft);
  line-height: 1.7;
  max-width: 560px;
}

/* ── 贰 · 检索式卡片目录 ── */
.query-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}

@media (max-width: 1100px) {
  .query-grid {
    grid-template-columns: 1fr;
  }
}

/* 目录卡片:顶部短粗条(中文赭金 / 英文黛青),像档案目录卡 */
.query-card {
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: #fffef8;
  display: flex;
  flex-direction: column;
  min-width: 0;
  box-shadow: var(--shadow-sheet);
}
.query-card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 10px 14px 8px;
  border-top: 3px solid var(--en);
  background: var(--en-bg);
}
.query-card.is-cn .query-card-head {
  border-top-color: var(--zh);
  background: var(--zh-bg);
}
.query-db {
  font-family: var(--font-display);
  font-size: 14.5px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--ink);
}
.query-alt {
  font-size: 11px;
  color: var(--ink-faint);
  letter-spacing: 0.12em;
}
.query-strategy {
  padding: 6px 14px 0;
  font-size: 11.5px;
  color: var(--ink-faint);
  font-family: var(--font-annotate);
}
.query-code {
  flex: 1;
  margin: 8px 0 0;
  padding: 10px 14px 12px;
  font-size: 12px;
  line-height: 1.65;
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 260px;
  overflow-y: auto;
  font-family: var(--font-mono);
}

/* 子检索式列表:更小字号,缩进展示,区别于主检索式 */
.query-sub {
  flex: none;
  border-top: 1px dashed var(--rule-soft);
}
.query-sub :deep(.el-collapse-item__header) {
  padding: 0 14px;
  font-size: 12px;
}
.query-sub :deep(.el-collapse-item__content) {
  padding-bottom: 10px;
}
.query-code-sub {
  flex: none;
  font-size: 11px;
  padding: 5px 14px;
  max-height: none;
}

/* ── 叁 · 三库进度 ── */
.prog-list {
  display: grid;
  gap: 4px;
}
.prog-row {
  padding: 10px 2px 12px;
}
.prog-row + .prog-row {
  border-top: 1px dashed var(--rule-soft);
}
.prog-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}
.prog-name {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--ink);
}
.prog-row.is-cn .prog-name::before {
  content: '';
  display: inline-block;
  width: 8px;
  height: 8px;
  background: var(--zh);
  margin-right: 7px;
  border-radius: 1px;
}
.prog-row.is-en .prog-name::before {
  content: '';
  display: inline-block;
  width: 8px;
  height: 8px;
  background: var(--en);
  margin-right: 7px;
  border-radius: 1px;
}
.prog-count {
  font-size: 12px;
  color: var(--ink-faint);
}
.prog-count .figure-num {
  font-size: 16px;
  color: var(--ink);
}
.prog-help {
  font-size: 12px;
  color: var(--ink-ghost);
  cursor: help;
}
.prog-help:hover {
  color: var(--cinnabar);
}
.prog-state {
  margin-left: auto;
  font-size: 11.5px;
  letter-spacing: 0.1em;
  padding: 2px 9px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}
.st-ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok); }
.st-warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
.st-bad { color: var(--bad); background: var(--bad-bg); border-color: var(--bad); }
.st-run { color: var(--cinnabar); background: var(--cinnabar-tint); border-color: var(--cinnabar); }
.st-wait { color: var(--ink-soft); background: var(--paper-deep); border-color: var(--rule-strong); }
.st-idle { color: var(--ink-ghost); background: transparent; border-color: var(--rule); }

.prog-log {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 8px;
}
.prog-log-label {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--ink-faint);
  border: 1px solid var(--rule);
  border-radius: var(--radius-sm);
  padding: 1px 7px;
  letter-spacing: 0.08em;
  background: var(--sheet-dim);
}
.prog-log-text {
  font-size: 12px;
  color: var(--ink-soft);
  line-height: 1.6;
  word-break: break-all;
  min-height: 20px;
}

/* ── 肆 · 收稿 ── */
.state-chip {
  font-size: 11.5px;
  letter-spacing: 0.1em;
  padding: 2px 9px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}

.harvest {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 10px 6px;
}
.harvest-num {
  font-size: 52px;
  line-height: 1;
  color: var(--cinnabar);
}
.harvest-label {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.1em;
}
.harvest-sub {
  margin-top: 4px;
  font-family: var(--font-annotate);
  font-size: 12px;
  color: var(--ink-faint);
}

.section-note-inline {
  font-size: 12px;
  color: var(--ink-faint);
}
</style>
