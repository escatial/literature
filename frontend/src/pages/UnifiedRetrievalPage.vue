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
import { ElMessage, ElMessageBox } from 'element-plus';
import { usePapersStore } from '@/stores/papers';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';
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
  clearPapers,
} from '@/api/endpoints';
import type { RetrievalTask } from '@/api/types';
import type { RetrievalHistory } from '@/api/endpoints';

const router = useRouter();

// 英文任务进度
const englishTask = ref<RetrievalTask | null>(null);
let pollTimer: number | null = null;

// 需求4:历史检索(最多 5 条)
const history = ref<RetrievalHistory[]>([]);

const refreshHistory = async () => {
  try {
    history.value = await listRetrievalHistory(5);
  } catch {
    /* 后端未启用时忽略 */
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
    ElMessage.success(`已加载历史文献 ${resp.total} 篇`);
    router.push('/pool');
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') {
      ElMessage.error(`查看失败:${String(e?.message ?? e)}`);
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
    ElMessage.success('已删除');
    await refreshHistory();
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') {
      ElMessage.error(`删除失败:${String(e?.message ?? e)}`);
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
// 用于解除 SSE 订阅时 keep EventSource 引用
const sseSources = new Map<string, EventSource>();

const ustore = useUnifiedRetrievalStore();
const papersStore = usePapersStore();
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
/** 需求1:成功导入文献池的文献数(本次任务,与 totalRetrieved 一致)。 */
const importedSuccess = computed(() => totalRetrieved.value);
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
  const cnkiDone = Object.values(cnkiTasks.value).every(
    (t) => t.stage === 'done' || t.stage === 'error',
  );
  const enDone = !englishTask.value || ['succeeded', 'failed'].includes(englishTask.value.status || '');
  return (Object.keys(cnkiTasks.value).length > 0 || !!englishTask.value) && cnkiDone && enDone;
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
  refreshHistory().catch(() => { /* 后端未启用时忽略 */ });
});

const hasNoResults = computed(() => tasksFinished.value && totalRetrieved.value === 0);
const hasTaskFailure = computed(
  () =>
    Object.values(cnkiTasks.value).some((t) => t.stage === 'error') ||
    englishTask.value?.status === 'failed',
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
  if (englishTask.value?.status === 'failed') {
    return englishTask.value.error || '英文任务失败';
  }
  const cnkiErr = Object.values(cnkiTasks.value).find((t) => t.stage === 'error');
  if (cnkiErr) {
    return cnkiErr.msg || cnkiErr.stage || '知网任务失败';
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
      } else {
        ustore.upsertCnkiTask(db, { ...initial, ...msg });
      }
      if (msg.stage === 'done' || msg.stage === 'error') {
        es.close();
        sseSources.delete(initial.task_id);
      }
    } catch {
      /* noop */
    }
  });
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
    percent = Math.min(99, Math.round(((task.progress_done ?? 0) / total) * 100));
  } else {
    percent = Math.min(99, Math.round(((task.saved ?? 0) / target) * 100));
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
      englishTask.value = t;
      // v4.1:把后端事件同步到 store,按 db 拆分 OpenAlex / PubMed 过程日志
      if (t.events && t.events.length) {
        ustore.ingestEnEvents(t.task_id, t.events);
      }
    } catch {
      englishTask.value = null;
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
  { key: 'cnki', name: '中国知网', tag: 'danger' as const, query: ustore.queriesCnki[0] || '', queries: ustore.queriesCnki },
  { key: 'openalex', name: 'OpenAlex', tag: 'success' as const, query: ustore.queriesOpenalex[0] || '', queries: ustore.queriesOpenalex },
  { key: 'pubmed', name: 'PubMed', tag: 'primary' as const, query: ustore.queriesPubmed[0] || '', queries: ustore.queriesPubmed },
]);

// ─────────────── 检索式生成 ───────────────
const generatePlan = async () => {
  const topic = ustore.topic.trim();
  if (!topic) {
    ElMessage.warning('请输入研究主题');
    return false;
  }
  try {
    ustore.setPlanning(true);
    const resp = await queryPlan(topic);
    ustore.applyPlan({
      topic_summary: resp.topic_summary || '',
      queries_cnki: resp.queries_cnki || [],
      queries_openalex: resp.queries_openalex || [],
      queries_pubmed: resp.queries_pubmed || [],
    });
    ElMessage.success('已生成 3 库 × 3 条检索式');
    return true;
  } catch (e) {
    ElMessage.error('生成检索式失败: ' + String(e));
    return false;
  } finally {
    ustore.setPlanning(false);
  }
};

// ─────────────── 一键全自动 ───────────────
const startUnifiedRetrieval = async () => {
  const topic = ustore.topic.trim();
  if (!topic) {
    ElMessage.warning('请输入研究主题');
    return;
  }
  // 自动勾选 pubmed + openalex
  const current = ustore.selectedDbs;
  const needEn = ['pubmed', 'openalex'].filter((d) => !current.includes(d));
  if (needEn.length) ustore.setDbs([...current, ...needEn]);

  // ★ 立即在 store 里占位一个 cnki 任务行 + 一条启动日志,
  //   保证用户点下按钮的那一瞬间就能看到「检索过程」面板出现,
  //   不需要等后端 HTTP 返回。
  const cnkiSelected = ustore.selectedDbs.includes('cnki');
  if (cnkiSelected) {
    ustore.initCnkiTask('cnki', topic);
    ustore.appendCnkiLog('cnki', '[检索式] 正在根据主题生成中英文概念组和知网专业检索式…');
  }
  const englishSelected = ustore.selectedDbs.includes('pubmed') || ustore.selectedDbs.includes('openalex');

  const planReady = await generatePlan();
  if (!planReady || !ustore.queriesCnki.length || !ustore.queriesOpenalex.length || !ustore.queriesPubmed.length) {
    if (cnkiSelected) {
      ustore.appendCnkiLog('cnki', '[错误] 检索式生成失败，已停止检索');
      ustore.upsertCnkiTask('cnki', {
        task_id: '', db_type: 'cnki' as const, stage: 'error',
      });
    }
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
    // ★ 新任务语义:每次检索都是独立任务,启动前清空文献池(历史已存在检索历史中)。
    //   这样文献池只反映「本次检索」的中英合并结果,不跨任务累积。
    await clearPapers();

    ElMessage.info('启动自动检索(知网 v4.0 + 英文 PubMed/OpenAlex)…');

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
        });
        ustore.appendCnkiLog('cnki', `[本地] 后端已分配任务 ${resp.task_id.slice(0, 8)}…,正在建立 SSE 订阅`);
        const initial = { task_id: resp.task_id, db_type: 'cnki' as const, stage: 'starting' };
        ustore.upsertCnkiTask('cnki', initial);
        subscribeCnki('cnki', initial);
      } catch (e: any) {
        const msg = String(e?.message ?? e);
        ustore.appendCnkiLog('cnki', `[错误] 调用 /api/cnki/start 失败: ${msg}`);
        ustore.upsertCnkiTask('cnki', {
          task_id: '', db_type: 'cnki' as const, stage: 'error',
        });
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
          sources: ['pubmed', 'openalex'].filter((d) => ustore.selectedDbs.includes(d)),
        });
        ustore.englishTaskId = resp.task_id || '';
        ustore.initEnTasks(resp.task_id);
        const enDbs = ['pubmed', 'openalex'].filter((d) => ustore.selectedDbs.includes(d));
        for (const d of enDbs) {
          ustore.appendEnLog(d as 'openalex' | 'pubmed', '[本地] 已点击「启动自动检索」,等待后端响应…');
        }
        const t = await getRetrievalTask(resp.task_id);
        englishTask.value = t;
        if (t.events && t.events.length) {
          ustore.ingestEnEvents(t.task_id, t.events);
        }
      } catch (e: any) {
        ElMessage.error(`英文检索任务启动失败: ${String(e?.message ?? e)}`);
      }
    };

    await Promise.allSettled([startCnkiTask(), startEnglishTask()]);
    startProgressPolling();
  } catch (e: any) {
    ElMessage.error('启动失败: ' + String(e?.message ?? e));
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
    ElMessage.success('已发送停止指令,正在终止所有检索任务');
  } catch (e: any) {
    ElMessage.error(`停止失败: ${String(e?.message ?? e)}`);
  } finally {
    stopping.value = false;
  }
};

onMounted(async () => {
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
      englishTask.value = null;
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
        placeholder="研究主题,如:无人机协同配送应急物资"
        style="flex: 1; min-width: 320px"
        clearable
        @keyup.enter="startUnifiedRetrieval"
      />
      <el-button type="primary" :loading="isRunning" :disabled="isRunning" @click="startUnifiedRetrieval">
        启动自动检索
      </el-button>
      <el-button type="danger" plain :loading="stopping" :disabled="!isRunning" @click="stopAll">
        停止
      </el-button>
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

  <!-- 完成态(需求1:总数 / 成功导入数 / 异常条目) -->
  <el-empty v-if="hasNoResults" description="未检索到有效文献,可调整主题或放宽检索式" />
  <el-card v-else-if="tasksFinished" shadow="never" style="margin-top: 16px">
    <template #header>
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <el-tag :type="hasTaskFailure ? 'danger' : hasFailureEntries ? 'warning' : 'success'" size="default">
          {{ hasTaskFailure ? '任务失败' : hasFailureEntries ? '部分异常' : '已完成' }}
        </el-tag>
        <span style="font-weight: 500">本次检索结果汇总</span>
      </div>
    </template>
    <el-row :gutter="16">
      <el-col :span="8">
        <div class="metric">
          <div class="metric-label">检索总数量</div>
          <div class="metric-value">{{ totalRetrieved }}</div>
          <div class="metric-sub">篇</div>
        </div>
      </el-col>
      <el-col :span="8">
        <div class="metric">
          <div class="metric-label">成功导入文献池</div>
          <div class="metric-value metric-success">{{ importedSuccess }}</div>
          <div class="metric-sub">篇</div>
        </div>
      </el-col>
      <el-col :span="8">
        <div class="metric">
          <div class="metric-label">异常条目</div>
          <div class="metric-value" :class="hasFailureEntries ? 'metric-danger' : 'metric-muted'">
            {{ failureEntries.length }}
          </div>
          <div class="metric-sub">条</div>
        </div>
      </el-col>
    </el-row>
    <el-collapse v-if="hasFailureEntries" style="margin-top: 12px">
      <el-collapse-item title="查看异常明细" name="fail-detail">
        <el-table :data="failureEntries" stripe size="small">
          <el-table-column prop="source" label="数据源" width="120" />
          <el-table-column prop="message" label="异常描述" />
        </el-table>
      </el-collapse-item>
    </el-collapse>
    <el-alert
      v-if="hasTaskFailure"
      type="error"
      :closable="false"
      show-icon
      style="margin-top: 12px"
      :title="taskErrorMessage || '任务失败'"
    />
    <el-alert
      v-else-if="hasTaskWarning"
      type="warning"
      :closable="false"
      show-icon
      style="margin-top: 12px"
      :title="`部分源异常,已自动忽略:${taskWarningMessage}`"
    />
  </el-card>

  <!-- 需求4:最近 5 条历史检索 -->
  <el-card v-if="history.length" shadow="never" style="margin-top: 16px">
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span>最近检索记录({{ history.length }} 条)</span>
        <el-button size="small" link @click="refreshHistory">刷新</el-button>
      </div>
    </template>
    <el-table :data="history" stripe size="small">
      <el-table-column label="检索时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="检索关键词" min-width="240">
        <template #default="{ row }">
          <div style="font-weight: 500">{{ row.topic }}</div>
          <div style="color: #909399; font-size: 12px">
            数据源:{{ (row.sources || []).join(', ') || '—' }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="文献总数" width="100" prop="total_count" />
      <el-table-column label="异常源" width="180">
        <template #default="{ row }">
          <span v-if="!Object.keys(row.failed_sources || {}).length" style="color: #67c23a">无</span>
          <el-tag
            v-for="(cnt, src) in row.failed_sources"
            :key="src"
            size="small"
            type="danger"
            style="margin-right: 4px"
          >
            {{ src }}: {{ cnt }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <el-button
            size="small"
            type="primary"
            link
            @click="viewHistory(row)"
          >
            查看
          </el-button>
          <el-button
            size="small"
            type="danger"
            link
            :loading="deletingId === row.id"
            :disabled="deletingId !== null"
            @click="removeHistory(row)"
          >
            删除
          </el-button>
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
