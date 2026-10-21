<script setup lang="ts">
/** 爬虫监控面板:任务启停 / 状态查询 / 参数热调整 / 资源面(动态池·断路器·代理池·告警)。
 *  数据源:后端 /api/crawler/*(backend/src/api/crawler_admin.py),轮询刷新。*/
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  ElButton,
  ElCard,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInputNumber,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus';
import { toast } from '@/utils/toast';
import {
  getCrawlerDashboard,
  getCrawlerTask,
  listCrawlerTasks,
  resetCrawlerBreaker,
  stopAllCrawlerTasks,
  stopCrawlerTask,
  updateCrawlerTaskParams,
} from '@/api/crawlerAdmin';
import type {
  AlertItem,
  BreakerSnapshot,
  CrawlerDashboard,
  CrawlerParamsPatch,
  CrawlerTaskDetail,
  CrawlerTaskStatus,
  CrawlerTaskSummary,
  PoolSnapshot,
  ProxySnapshot,
} from '@/api/crawlerAdmin';

// ─── 轮询与首屏聚合 ─────────────────────────────────────────

const autoRefresh = ref(true);
const POLL_MS = 5000;
let timer: ReturnType<typeof setInterval> | null = null;

const dashboard = ref<CrawlerDashboard | null>(null);
const tasks = ref<CrawlerTaskSummary[]>([]);
const statusFilter = ref<CrawlerTaskStatus | ''>('');
const loading = ref(false);

const loadAll = async () => {
  loading.value = true;
  try {
    const [dash, list] = await Promise.all([
      getCrawlerDashboard(),
      listCrawlerTasks(statusFilter.value || undefined),
    ]);
    dashboard.value = dash;
    tasks.value = list;
  } catch {
    /* http 拦截器已弹错(网络/5xx),静默避免轮询刷屏 */
  } finally {
    loading.value = false;
  }
};

const startPolling = () => {
  stopPolling();
  if (autoRefresh.value) timer = setInterval(loadAll, POLL_MS);
};
const stopPolling = () => {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
};

watch(autoRefresh, (v) => (v ? startPolling() : stopPolling()));
watch(statusFilter, loadAll);

onMounted(async () => {
  await loadAll();
  startPolling();
});
onBeforeUnmount(stopPolling);

// ─── 展示辅助 ───────────────────────────────────────────────

const stats = computed(() => dashboard.value?.tasks ?? null);
const pool = computed<PoolSnapshot | null>(() => dashboard.value?.pool ?? null);
const breakers = computed<BreakerSnapshot[]>(() => dashboard.value?.breakers ?? []);
const proxy = computed<ProxySnapshot | null>(() => dashboard.value?.proxy ?? null);
const alerts = computed<AlertItem[]>(() => dashboard.value?.alerts ?? []);

type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info';

const STATUS_TAG: Record<string, TagType> = { running: 'primary', done: 'success', failed: 'danger', stopped: 'warning', queued: 'info' };
const STATUS_LABEL: Record<string, string> = { running: '运行中', done: '完成', failed: '失败', stopped: '已停止', queued: '排队' };
const BREAKER_TAG: Record<string, TagType> = { closed: 'success', half_open: 'warning', open: 'danger' };
const BREAKER_LABEL: Record<string, string> = { closed: '闭合', half_open: '半开', open: '熔断' };

const statusTag = (s: string): TagType => STATUS_TAG[s] ?? 'info';
const statusLabel = (s: string): string => STATUS_LABEL[s] ?? s;
const breakerTag = (s: string): TagType => BREAKER_TAG[s] ?? 'info';
const breakerLabel = (s: string): string => BREAKER_LABEL[s] ?? s;

const fmtTs = (ts: number) => (ts ? new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—');
const fmtElapsed = (sec: number) => {
  if (!sec) return '—';
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
};
const fmtPercent = (v?: number | null) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`);

// ─── 任务控制:启停 / 详情 / 参数热调整 ────────────────────────

const stopOne = async (row: CrawlerTaskSummary) => {
  try {
    await ElMessageBox.confirm(`确定停止任务 ${row.task_id}?`, '停止任务', { type: 'warning' });
  } catch {
    return; // 用户取消
  }
  await stopCrawlerTask(row.task_id);
  toast.success('已发出停止指令');
  await loadAll();
};

const stopEverything = async () => {
  try {
    await ElMessageBox.confirm('确定停止全部运行中任务?(运维兜底动作)', '停止全部', { type: 'warning' });
  } catch {
    return;
  }
  const r = await stopAllCrawlerTasks();
  toast.success(`已停止 ${r.stopped} 个任务`);
  await loadAll();
};

// 详情抽屉
const drawerVisible = ref(false);
const detail = ref<CrawlerTaskDetail | null>(null);
const detailLoading = ref(false);

const openDetail = async (row: CrawlerTaskSummary) => {
  drawerVisible.value = true;
  await loadDetail(row.task_id);
};

const loadDetail = async (taskId: string) => {
  detailLoading.value = true;
  try {
    detail.value = await getCrawlerTask(taskId, true);
  } catch (e) {
    // v9.6:此前无 catch,接口失败直接 unhandled rejection,抽屉空白无提示
    console.error('[crawler-monitor] 加载任务详情失败:', e);
    toast.error('加载任务详情失败');
  } finally {
    detailLoading.value = false;
  }
};

// 参数热调整表单(空值表示不调整该项;后端白名单校验,越权 422)
const EMPTY_PATCH = { delay_seconds: undefined, max_workers: undefined, page_size: undefined, max_per_keyword: undefined };
const paramForm = ref<{ [K in keyof CrawlerParamsPatch]: number | undefined }>({ ...EMPTY_PATCH });

const applyParams = async () => {
  if (!detail.value) return;
  const patch = Object.fromEntries(
    Object.entries(paramForm.value).filter(([, v]) => v !== undefined && v !== null),
  );
  if (!Object.keys(patch).length) {
    toast.warning('请至少填写一个要调整的参数');
    return;
  }
  try {
    await updateCrawlerTaskParams(detail.value.task_id, patch as CrawlerParamsPatch);
    toast.success('参数已下发并即时生效');
    paramForm.value = { ...EMPTY_PATCH };
    await loadDetail(detail.value.task_id);
    await loadAll();
  } catch (e) {
    // v9.6:此前无 catch,下发失败(422/500)用户毫无感知,误以为已生效
    console.error('[crawler-monitor] 参数下发失败:', e);
    toast.error('参数下发失败,请检查后重试');
  }
};

// 断路器手动复位(open 卡死时的运维动作)
const doResetBreaker = async (b: BreakerSnapshot) => {
  try {
    await ElMessageBox.confirm(`复位断路器 ${b.name}?`, '复位', { type: 'warning' });
  } catch {
    return;
  }
  await resetCrawlerBreaker(b.name);
  toast.success(`断路器 ${b.name} 已复位`);
  await loadAll();
};
</script>

<template>
  <div class="crawler-page">
    <!-- 工具条 -->
    <div class="toolbar">
      <span class="section-no">附</span>
      <span class="section-title">爬虫监控</span>
      <el-switch v-model="autoRefresh" active-text="自动刷新(5s)" />
      <el-button :loading="loading" @click="loadAll">手动刷新</el-button>
      <el-button type="danger" plain @click="stopEverything">停止全部任务</el-button>
    </div>

    <!-- 任务概览:账目式六栏 -->
    <div v-if="stats" class="stat-row">
      <div class="stat-cell">
        <div class="stat-num figure-num">{{ stats.total }}</div>
        <div class="stat-label">总任务</div>
      </div>
      <div class="stat-cell">
        <div class="stat-num figure-num running">{{ stats.running }}</div>
        <div class="stat-label">运行中</div>
      </div>
      <div class="stat-cell">
        <div class="stat-num figure-num ok">{{ stats.done }}</div>
        <div class="stat-label">完成</div>
      </div>
      <div class="stat-cell">
        <div class="stat-num figure-num bad">{{ stats.failed }}</div>
        <div class="stat-label">失败</div>
      </div>
      <div class="stat-cell">
        <div class="stat-num figure-num">{{ stats.stopped }}</div>
        <div class="stat-label">已停止</div>
      </div>
      <div class="stat-cell">
        <div class="stat-num figure-num">{{ stats.saved_total }}</div>
        <div class="stat-label">累计入库</div>
      </div>
    </div>

    <!-- 资源面:动态池 / 断路器 / 代理池 / 告警 -->
    <div class="grid-2">
      <el-card shadow="never">
        <template #header><span class="card-head">动态并发池</span></template>
        <template v-if="pool?.initialized">
          <div class="kv"><span>当前并发 / 上下限</span><b>{{ pool.workers }} / {{ pool.min_workers }}~{{ pool.max_workers }}</b></div>
          <div class="kv"><span>平均延迟</span><b>{{ pool.avg_latency }}s</b></div>
          <div class="kv"><span>窗口失败率</span><b>{{ fmtPercent(pool.fail_rate) }}</b></div>
          <div class="kv"><span>CPU 负载</span><b>{{ pool.psutil_available ? fmtPercent(pool.cpu_load) : 'psutil 未安装' }}</b></div>
          <div class="kv"><span>风险挂起</span><b>{{ pool.risk_pending }}</b></div>
        </template>
        <span v-else class="muted">池未初始化(暂无爬取任务)</span>
      </el-card>

      <el-card shadow="never">
        <template #header><span class="card-head">断路器</span></template>
        <el-table v-if="breakers.length" :data="breakers" size="small">
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="breakerTag(row.state)" size="small">{{ breakerLabel(row.state) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="consecutive_failures" label="连败" width="70" />
          <el-table-column label="冷却" width="80">
            <template #default="{ row }">{{ row.cooldown_left ? `${row.cooldown_left}s` : '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="doResetBreaker(row as BreakerSnapshot)">复位</el-button>
            </template>
          </el-table-column>
        </el-table>
        <span v-else class="muted">暂无断路器注册</span>
      </el-card>

      <el-card shadow="never">
        <template #header><span class="card-head">代理池(模式: {{ proxy?.mode ?? '—' }})</span></template>
        <el-table v-if="proxy?.proxies?.length" :data="proxy.proxies" size="small">
          <el-table-column prop="url" label="代理(打码)" min-width="150" show-overflow-tooltip />
          <el-table-column prop="score" label="健康分" width="80" />
          <el-table-column label="可用" width="70">
            <template #default="{ row }">
              <el-tag :type="row.available ? 'success' : 'danger'" size="small">{{ row.available ? '是' : '否' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="last_latency" label="延迟(s)" width="85" />
          <el-table-column label="冷却" width="80">
            <template #default="{ row }">{{ row.cooldown_left ? `${row.cooldown_left}s` : '—' }}</template>
          </el-table-column>
        </el-table>
        <span v-else class="muted">当前模式无代理实例(off 模式或池为空)</span>
      </el-card>

      <el-card shadow="never">
        <template #header><span class="card-head">最近告警</span></template>
        <template v-if="alerts.length">
          <div v-for="(a, i) in alerts" :key="i" class="alert-item">
            <el-tag :type="a.level === 'critical' ? 'danger' : 'warning'" size="small">{{ a.level }}</el-tag>
            <span class="alert-title">{{ a.title }}</span>
            <span class="alert-meta">×{{ a.count }} · {{ fmtTs(a.ts) }}</span>
            <div class="alert-detail">{{ a.detail }}</div>
          </div>
        </template>
        <span v-else class="muted">无告警</span>
      </el-card>
    </div>

    <!-- 任务列表 -->
    <el-card shadow="never">
      <template #header><span class="card-head">爬取任务</span></template>
      <div class="table-toolbar">
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 140px">
          <el-option label="运行中" value="running" />
          <el-option label="排队" value="queued" />
          <el-option label="完成" value="done" />
          <el-option label="失败" value="failed" />
          <el-option label="已停止" value="stopped" />
        </el-select>
      </div>
      <el-table :data="tasks" size="small" @row-dblclick="(row) => openDetail(row as CrawlerTaskSummary)">
        <el-table-column prop="task_id" label="任务ID" min-width="170" show-overflow-tooltip />
        <el-table-column prop="query" label="检索式" min-width="160" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="stage" label="阶段" min-width="100" show-overflow-tooltip />
        <el-table-column prop="saved" label="入库" width="70" />
        <el-table-column prop="skipped" label="跳过" width="70" />
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ fmtElapsed(row.elapsed) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row as CrawlerTaskSummary)">详情</el-button>
            <el-button
              v-if="row.status === 'running' || row.status === 'queued'"
              link type="danger" size="small" @click="stopOne(row as CrawlerTaskSummary)"
            >停止</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 任务详情抽屉:计数 / 质量 / 参数热调整 / 环形日志 -->
    <el-drawer v-model="drawerVisible" :title="`任务详情 ${detail?.task_id ?? ''}`" size="560px">
      <div v-if="detail" v-loading="detailLoading">
        <div class="detail-head">
          <el-tag :type="statusTag(detail.status)">{{ statusLabel(detail.status) }}</el-tag>
          <span class="muted">{{ detail.stage || '—' }} · 耗时 {{ fmtElapsed(detail.elapsed) }}</span>
        </div>
        <div v-if="detail.error" class="detail-error">{{ detail.error }}</div>

        <h4>全链路计数</h4>
        <div class="counter-grid">
          <div v-for="(v, k) in detail.counters" :key="k" class="counter-cell">
            <div class="counter-num figure-num">{{ v }}</div>
            <div class="counter-name">{{ k }}</div>
          </div>
        </div>

        <h4>数据质量</h4>
        <div class="kv"><span>已评样本 / 平均分 / 最低分</span><b>{{ detail.quality.count }} / {{ detail.quality.avg_score }} / {{ detail.quality.min_score }}</b></div>
        <div v-if="Object.keys(detail.quality.flags).length" class="flags">
          <el-tag v-for="(n, f) in detail.quality.flags" :key="f" type="warning" size="small" class="flag-tag">
            {{ f }} ×{{ n }}
          </el-tag>
        </div>

        <h4>参数热调整</h4>
        <el-form inline>
          <el-form-item label="请求间隔(s)">
            <el-input-number v-model="paramForm.delay_seconds" :min="0.5" :step="0.5" :controls="false" style="width: 100px" />
          </el-form-item>
          <el-form-item label="并发上限">
            <el-input-number v-model="paramForm.max_workers" :min="1" :max="16" :controls="false" style="width: 100px" />
          </el-form-item>
          <el-form-item label="页大小">
            <el-input-number v-model="paramForm.page_size" :min="10" :max="50" :controls="false" style="width: 100px" />
          </el-form-item>
          <el-form-item label="单式上限">
            <el-input-number v-model="paramForm.max_per_keyword" :min="1" :controls="false" style="width: 100px" />
          </el-form-item>
        </el-form>
        <div class="kv"><span>当前生效</span><b>{{ JSON.stringify(detail.params) }}</b></div>
        <div class="detail-actions">
          <el-button type="primary" size="small" @click="applyParams">下发调整</el-button>
          <el-button size="small" @click="loadDetail(detail.task_id)">刷新详情</el-button>
          <el-button
            v-if="detail.status === 'running' || detail.status === 'queued'"
            type="danger" size="small" plain @click="stopOne(detail as unknown as CrawlerTaskSummary)"
          >停止任务</el-button>
        </div>

        <h4>任务日志(最近 500 条)</h4>
        <pre class="logs">{{ detail.logs?.length ? detail.logs.join('\n') : '暂无日志' }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.crawler-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.toolbar {
  display: flex;
  align-items: baseline;
  gap: 16px;
}
.toolbar .el-switch {
  align-self: center;
}

/* 概览:墨线账目栏 */
.stat-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
}
@media (max-width: 960px) {
  .stat-row {
    grid-template-columns: repeat(3, 1fr);
  }
}
.stat-cell {
  padding: 10px 14px 12px;
  background: var(--sheet);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--rule-strong);
  border-radius: var(--radius);
}
.stat-num {
  font-size: 30px;
  line-height: 1.2;
  color: var(--ink);
}
.stat-num.running { color: var(--cinnabar); }
.stat-num.ok { color: var(--ok); }
.stat-num.bad { color: var(--bad); }
.stat-label {
  font-size: 12px;
  color: var(--ink-faint);
  margin-top: 3px;
  letter-spacing: 0.06em;
}

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
@media (max-width: 960px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
}
.card-head {
  font-family: var(--font-display);
  letter-spacing: 0.08em;
}

.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12.5px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--rule-soft);
}
.kv span {
  color: var(--ink-faint);
}
.kv b {
  font-family: var(--font-display);
  color: var(--ink);
}
.muted {
  font-family: var(--font-annotate);
  color: var(--ink-faint);
  font-size: 13px;
}
.table-toolbar {
  margin-bottom: 12px;
}
.alert-item {
  padding: 7px 0;
  border-bottom: 1px dashed var(--rule-soft);
  font-size: 13px;
}
.alert-item:last-child {
  border-bottom: none;
}
.alert-title {
  margin-left: 8px;
  color: var(--ink);
}
.alert-meta {
  float: right;
  color: var(--ink-faint);
  font-size: 12px;
}
.alert-detail {
  color: var(--ink-faint);
  font-size: 12px;
  margin-top: 3px;
  word-break: break-all;
}
.detail-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.detail-error {
  margin-top: 10px;
  padding: 9px 12px;
  background: var(--bad-bg);
  color: var(--bad);
  border-left: 3px solid var(--bad);
  border-radius: var(--radius-sm);
  font-size: 13px;
  word-break: break-all;
}
.counter-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.counter-cell {
  background: var(--sheet-dim);
  border: 1px solid var(--rule-soft);
  border-radius: var(--radius-sm);
  padding: 8px;
  text-align: center;
}
.counter-num {
  font-size: 18px;
  color: var(--ink);
}
.counter-name {
  font-size: 11px;
  color: var(--ink-faint);
  margin-top: 2px;
}
.flags {
  margin-top: 8px;
}
.flag-tag {
  margin-right: 6px;
}
.detail-actions {
  margin-top: 10px;
}
/* 印房日志:墨底纸字 */
.logs {
  background: #2b2418;
  color: #e8dfc9;
  padding: 14px;
  border-radius: var(--radius);
  font-size: 12px;
  line-height: 1.7;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: var(--font-mono);
}
h4 {
  margin: 18px 0 8px;
  font-family: var(--font-display);
  color: var(--ink);
  letter-spacing: 0.08em;
  font-size: 14px;
}
h4::before {
  content: '· ';
  color: var(--cinnabar);
}
</style>
