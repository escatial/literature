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
import { QuestionFilled } from '@element-plus/icons-vue';
import { useRouter } from 'vue-router';
import { ElMessageBox } from 'element-plus';
import { toast } from '@/utils/toast';
import { usePapersStore } from '@/stores/papers';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';
import { useLLMProvidersStore } from '@/stores/llmProviders';
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
  return {
    key: 'cnki',
    name: '中国知网',
    tag: 'danger',
    percent,
    status: task.stage === 'error' ? ('exception' as const) : task.stage === 'done' ? ('success' as const) : undefined,
    saved: task.saved ?? 0,
    target,
    running: task.stage !== 'done' && task.stage !== 'error',
    lastLog: logs[logs.length - 1] ?? '',
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
  // Pinia store 会从 localStorage 恢复任务;同步到本页内存状态,确保刷新后仍显示完成汇总。
  runStarted.value = Boolean(
    ustore.englishTaskId || Object.keys(ustore.cnkiTasks).length,
  );
  // v7.1:加载 LLM provider 状态(后台异步,不阻塞主题输入)
  llmProviders.refresh().catch(() => { /* 接口挂了也不挡用户 */ });
  await refreshHistory();
  // 恢复英文任务进度
  if (ustore.englishTaskId) {
    try {
      const t = await getRetrievalTask(ustore.englishTaskId);
      englishTask.value = t;
      // v4.1:把已落库的事件同步到 enTasks,刷新后也能看到 OpenAlex / PubMed 过程日志
      if (t.events && t.events.length) {
        ustore.ingestEnEvents(t.task_id, t.events);
      }
      startProgressPolling();
    } catch {
      // v9.6:恢复失败不放弃——占住 task_id 让轮询接手自动重试
      // (englishTask 仅缺 status/events,refreshProgress 首次成功即整包覆盖)
      console.warn('[检索] 恢复英文任务状态失败,交由轮询自动重试');
      englishTask.value = { task_id: ustore.englishTaskId } as RetrievalTask;
      startProgressPolling();
    }
  }
  // 恢复未完结的知网任务 SSE(切 tab 后回来)
  for (const [db, row] of Object.entries(cnkiTasks.value)) {
    if (row.stage === 'done' || row.stage === 'error') continue;
    if (!row.task_id) continue;
    subscribeCnki(db, row);
  }
});

onBeforeUnmount(() => {
  stopProgressPolling();
  // SSE 不立刻关闭,后台仍继续推消息;事件触发 onprogress 会再写 store
  // 切换路由回来时 onMounted 会重连,避免双重订阅靠 task_id 去重
});
</script>

<template>
  <!-- 第一栏:主题输入 -->
  <el-card shadow="never">
    <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap">
      <el-input
        v-model="topicInput"
        placeholder="研究主题"
        style="flex: 1; min-width: 320px"
        clearable
        :disabled="isRunning"
      />
      <el-tag v-if="ustore.planning" type="primary" effect="plain">
        Agent 正在自动生成三库检索式…
      </el-tag>
      <el-tag v-else-if="isRunning" type="success" effect="plain">
        Agent 正在自动检索
      </el-tag>
      <el-button
        type="primary"
        :loading="ustore.planning"
        :disabled="isRunning || !topicInput.trim()"
        @click="startUnifiedRetrieval"
      >
        {{ '立即启动' }}
      </el-button>
      <el-button type="danger" plain :loading="stopping" :disabled="!isRunning" @click="stopAll">
        停止
      </el-button>
    </div>

    <!-- v7.1 LLM provider 状态条:展示当前默认/备用,避免用户在 LLM 不通时疑惑 -->
    <div class="llm-providers-bar">
      <span class="llm-providers-label">LLM:</span>
      <el-tag
        v-for="p in llmProviders.providers"
        :key="p.id"
        :type="p.is_default ? 'success' : (p.is_active_fallback ? 'warning' : 'info')"
        :effect="p.is_default ? 'dark' : 'plain'"
        size="small"
      >
        {{ p.label }}
        <span v-if="p.is_default">·推荐</span>
        <span v-else-if="p.is_active_fallback">·备用</span>
        <span v-else>·配置保留</span>
      </el-tag>
      <el-tooltip
        v-if="llmProviders.providers.length"
        placement="top"
        :content="`轮换顺序:${llmProviders.fallbackOrder.join(' → ') || '(空)'}`"
      >
        <span class="llm-providers-hint">ⓘ</span>
      </el-tooltip>
    </div>
  </el-card>

  <!-- 第二栏:检索式展示区(3 个等宽子栏) -->
  <el-card shadow="never" style="margin-top: 16px">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px">
        <span>检索式展示区</span>
        <span style="font-size: 12px; color: #909399">
          实际按拆分式逐条遍历检索,结果合并去重;零结果时继续自动放宽
        </span>
      </div>
    </template>
    <div class="query-grid">
      <div v-for="item in planPreview" :key="item.key" class="query-cell">
        <div class="query-cell-head">
          <el-tag :type="item.tag" effect="light" size="small">{{ item.name }}</el-tag>
        </div>
        <div v-if="item.queries.length > 1" class="query-strategy">
          当前展示第 1 条实际执行式,共 {{ item.queries.length }} 条候选式
        </div>
        <pre class="query-code">{{ item.query || '—' }}</pre>
        <!-- 英文长检索式按语义单元拆分的子检索式(依次执行、合并去重) -->
        <el-collapse v-if="item.queries.length > 1" style="margin-top: 8px">
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

  <!-- 第三~五栏:三库进度(统一结构,样式完全一致,始终渲染) -->
  <template v-for="bar in progressBars" :key="bar.key">
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div class="progress-header">
          <div class="progress-title">
            <el-tag :type="bar.tag" effect="light" size="small">{{ bar.name }}</el-tag>
            <span class="progress-count">{{ bar.saved }} 篇</span>
            <el-tooltip
              v-if="bar.key !== 'cnki'"
              content="该数字为该源单源累加入池数;三库累加 ≠ 「检索总数量」是预期的跨源去重效应(同 lit_id 跨多源 / 跨子检索式只算一次)"
              placement="top"
            >
              <el-icon class="progress-help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <el-tag :type="bar.status === 'exception' ? 'danger' : bar.status === 'success' ? 'success' : 'warning'" size="small">
            {{ bar.status === 'exception' ? '失败' : bar.status === 'success' ? '已完成' : bar.running ? '进行中' : '待开始' }}
          </el-tag>
        </div>
      </template>
      <el-progress
        :percentage="bar.percent"
        :status="bar.status"
        :stroke-width="14"
        style="margin: 4px 0 12px"
      />
      <div class="last-log">
        <span class="last-log-label">最新日志</span>
        <span class="last-log-text">{{ bar.lastLog || '等待开始…' }}</span>
      </div>
    </el-card>
  </template>

  <el-card v-if="hasNoResults" shadow="never" style="margin-top: 16px">
    <template #header><span>本次检索结果汇总</span></template>
    <el-empty description="未检索到有效文献,可调整主题或放宽检索式" :image-size="70" />
  </el-card>
  <el-card v-else-if="tasksFinished" shadow="never" style="margin-top: 16px">
    <template #header>
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <el-tag :type="hasTaskFailure ? 'danger' : hasFailureEntries ? 'warning' : 'success'">
          {{ hasTaskFailure ? '任务失败' : hasFailureEntries ? '部分异常' : '已完成' }}
        </el-tag>
        <span style="font-weight: 500">本次检索结果汇总</span>
      </div>
    </template>
    <!-- v7.2:用户只关心最后入库多少——只展示「入库文献」一个数字,
         与文献池页面严格同口径(任务完成后取池 API 实际 total)。
         检索总数/拦截数等中间口径不再展示,差异全部沉淀在质量闸门内部。 -->
    <el-row :gutter="16">
      <el-col :span="24">
        <div class="metric">
          <div class="metric-label">入库文献</div>
          <div class="metric-value metric-success">{{ importedSuccess }}</div>
          <div class="metric-sub">篇</div>
        </div>
      </el-col>
    </el-row>
    <el-alert v-if="hasTaskFailure" type="error" :closable="false" show-icon style="margin-top: 12px" :title="taskErrorMessage || '任务失败'" />
    <el-alert v-else-if="hasTaskWarning" type="warning" :closable="false" show-icon style="margin-top: 12px" :title="`部分源异常,已自动忽略:${taskWarningMessage}`" />
  </el-card>

  <el-card shadow="never" style="margin-top: 16px">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span>最近检索记录（{{ history.length }} 条）</span>
        <el-button size="small" link @click="refreshHistory">刷新</el-button>
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
      <el-table-column label="检索关键词" min-width="240" prop="topic" />
      <el-table-column label="文献总数" width="100" prop="total_count" />
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
.metric {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  background: #fafafa;
}
.metric-label { color: #909399; font-size: 13px; margin-bottom: 8px; }
.metric-value { font-size: 28px; font-weight: 600; color: #303133; line-height: 1.2; }
.metric-success { color: #67c23a; }
.metric-danger { color: #f56c6c; }
.metric-muted { color: #c0c4cc; }
.metric-sub { color: #909399; font-size: 12px; margin-top: 4px; }

/* v7.1 LLM provider 状态条 */
.llm-providers-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed #ebeef5;
}
.llm-providers-label {
  font-size: 12px;
  color: #909399;
}
.llm-providers-hint {
  font-size: 13px;
  color: #909399;
  cursor: help;
}
</style>

<style scoped>
/* 检索式展示区:3 个等宽子栏 */
.query-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

@media (max-width: 1100px) {
  .query-grid {
    grid-template-columns: 1fr;
  }
}

.query-cell {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fafafa;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.query-cell-head {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid #ebeef5;
  background: #fff;
  border-radius: 6px 6px 0 0;
}

.query-code {
  flex: 1;
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
  color: #303133;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 260px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace;
}

/* 子检索式列表:更小字号,缩进展示,区别于主检索式 */
.query-code-sub {
  flex: none;
  font-size: 11px;
  padding: 6px 12px;
  max-height: none;
  border-top: 1px dashed #ebeef5;
}

/* 进度栏统一结构 */
.progress-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.progress-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.progress-count {
  font-size: 12px;
  color: #909399;
}

.progress-help {
  font-size: 14px;
  color: #c0c4cc;
  cursor: help;
}

.progress-help:hover {
  color: #409eff;
}

.last-log {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fafafa;
}

.last-log-label {
  flex-shrink: 0;
  font-size: 12px;
  color: #909399;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 1px 6px;
}

.last-log-text {
  font-size: 12px;
  color: #303133;
  line-height: 1.6;
  word-break: break-all;
  min-height: 20px;
}
</style>
