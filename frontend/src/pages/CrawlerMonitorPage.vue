<script setup lang="ts">
/** 监控与管理:爬虫监控 / 用户管理(管理员) / LLM 设置(按用户)。
 *  爬虫数据源:后端 /api/crawler/*(backend/src/api/crawler_admin.py),轮询刷新。
 *  用户与 LLM 配置:/api/auth/*(backend/src/api/auth.py)。*/
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  ElButton,
  ElCard,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTabs,
  ElTabPane,
  ElRadioButton,
  ElRadioGroup,
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
import { authApi, type AuthUser, type UpstreamModel } from '@/api/auth';
import { useAuthStore } from '@/stores/auth';
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

const auth = useAuthStore();
const route = useRoute();
/** 支持 /crawler?tab=llm 直达指定页签(检索页「去配置」入口) */
const _initialTab = route.query.tab === 'llm' ? 'llm'
  : route.query.tab === 'users' ? 'users' : 'crawler';
const activeTab = ref<'crawler' | 'users' | 'llm'>(_initialTab);

// ─── LLM 设置(按用户) ───────────────────────────────────────

const llmForm = ref({
  llm_kind: 'openai' as string,
  llm_base_url: '',
  llm_api_key: '',
  llm_model: '',
});
const llmSaving = ref(false);
const llmProbing = ref(false);
const modelOptions = ref<UpstreamModel[]>([]);

const llmKindLabel = computed(() =>
  llmForm.value.llm_kind === 'anthropic' ? 'Anthropic 兼容' : 'OpenAI 兼容');
const llmBaseUrlPlaceholder = computed(() =>
  llmForm.value.llm_kind === 'anthropic'
    ? '例如 https://api.anthropic.com 或 https://api.minimax.cn/anthropic'
    : '例如 https://api.minimaxi.com/v1 或 https://openrouter.ai/api/v1');
const llmKindHint = computed(() =>
  llmForm.value.llm_kind === 'anthropic'
    ? 'Anthropic Messages 协议:适用于 Claude 官方 API 及其兼容网关(含 MiniMax 的 anthropic 端点)。'
    : 'OpenAI Chat Completions 协议:适用于 MiniMax、DeepSeek、通义、OpenRouter、Gemini OpenAI 端点等绝大多数平台。');

const fillLlmForm = () => {
  const u = auth.user;
  if (!u) return;
  llmForm.value.llm_kind = u.llm_kind === 'anthropic' ? 'anthropic' : 'openai';
  llmForm.value.llm_base_url = u.llm_base_url || '';
  llmForm.value.llm_model = u.llm_model || '';
  llmForm.value.llm_api_key = '';
};

const saveLlm = async () => {
  llmSaving.value = true;
  try {
    const { data } = await authApi.saveLlmConfig({
      llm_kind: llmForm.value.llm_kind,
      llm_base_url: llmForm.value.llm_base_url.trim(),
      llm_api_key: llmForm.value.llm_api_key.trim(),
      llm_model: llmForm.value.llm_model.trim(),
    });
    auth.user = data.user;
    llmForm.value.llm_api_key = '';
    toast.success(data.message);
  } catch (e: any) {
    toast.error(String(e?.response?.data?.detail ?? e?.message ?? '保存失败'));
  } finally {
    llmSaving.value = false;
  }
};

const probeModels = async () => {
  if (!llmForm.value.llm_base_url.trim()) {
    toast.warning('请先填写 Base URL');
    return;
  }
  llmProbing.value = true;
  try {
    const { data } = await authApi.probeModels({
      llm_kind: llmForm.value.llm_kind,
      llm_base_url: llmForm.value.llm_base_url.trim(),
      llm_api_key: llmForm.value.llm_api_key.trim(),
    });
    modelOptions.value = data.models ?? [];
    toast.success(`获取到 ${modelOptions.value.length} 个模型`);
  } catch (e: any) {
    modelOptions.value = [];
    toast.error(String(e?.response?.data?.detail ?? e?.message ?? '获取模型列表失败'));
  } finally {
    llmProbing.value = false;
  }
};

// ─── 用户管理(管理员) ───────────────────────────────────────

const users = ref<AuthUser[]>([]);
const usersLoading = ref(false);

const USER_STATUS_TAG: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  approved: 'success', pending: 'warning', rejected: 'danger',
};
const USER_STATUS_LABEL: Record<string, string> = {
  approved: '已通过', pending: '待审核', rejected: '已拒绝',
};

const loadUsers = async () => {
  usersLoading.value = true;
  try {
    const { data } = await authApi.adminUsers();
    users.value = data.users;
  } catch {
    /* 拦截器已弹错 */
  } finally {
    usersLoading.value = false;
  }
};

watch(activeTab, (tab) => {
  if (tab === 'users' && auth.isAdmin && users.value.length === 0) void loadUsers();
});

const doApprove = async (u: AuthUser) => {
  await authApi.approve(u.user_id);
  toast.success(`已通过 ${u.username}`);
  await loadUsers();
};

const doReject = async (u: AuthUser) => {
  try {
    await ElMessageBox.confirm(`拒绝 ${u.username} 的注册申请?`, '拒绝注册', { type: 'warning' });
  } catch {
    return;
  }
  await authApi.reject(u.user_id);
  toast.success(`已拒绝 ${u.username}`);
  await loadUsers();
};

const doRemoveUser = async (u: AuthUser) => {
  try {
    await ElMessageBox.confirm(`删除用户 ${u.username}?该操作不可恢复。`, '删除用户', { type: 'warning' });
  } catch {
    return;
  }
  try {
    await authApi.removeUser(u.user_id);
    toast.success(`已删除 ${u.username}`);
    await loadUsers();
  } catch (e: any) {
    toast.error(String(e?.response?.data?.detail ?? e?.message ?? '删除失败'));
  }
};

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
  // 进入页面时确保会话用户信息就绪(LLM 表单回显依赖 auth.user)
  if (!auth.user) await auth.restore();
  fillLlmForm();
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
    <!-- 页头 -->
    <div class="toolbar">
      <span class="section-no">附</span>
      <span class="section-title">监控与管理</span>
    </div>

    <el-tabs v-model="activeTab" class="admin-tabs">
      <!-- ═══ Tab 1:爬虫监控 ═══ -->
      <el-tab-pane label="爬虫监控" name="crawler">
        <div class="tab-toolbar">
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
      </el-tab-pane>

      <!-- ═══ Tab 2:用户管理(仅管理员) ═══ -->
      <el-tab-pane v-if="auth.isAdmin" label="用户管理" name="users">
        <div class="tab-toolbar">
          <span class="tab-hint">新用户注册后处于「待审核」状态,审核通过才能登录。</span>
          <el-button :loading="usersLoading" @click="loadUsers">刷新</el-button>
        </div>
        <el-card shadow="never">
          <el-table :data="users" size="small" v-loading="usersLoading">
            <el-table-column prop="username" label="用户名" min-width="120" />
            <el-table-column prop="email" label="邮箱" min-width="160" show-overflow-tooltip />
            <el-table-column label="角色" width="90">
              <template #default="{ row }">
                <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" size="small">
                  {{ row.role === 'admin' ? '管理员' : '用户' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="USER_STATUS_TAG[row.status] ?? 'info'" size="small">
                  {{ USER_STATUS_LABEL[row.status] ?? row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="注册时间" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.created_at?.replace('T', ' ').slice(0, 19) ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="row.status === 'pending'"
                  link type="primary" size="small" @click="doApprove(row as AuthUser)"
                >通过</el-button>
                <el-button
                  v-if="row.status === 'pending'"
                  link type="warning" size="small" @click="doReject(row as AuthUser)"
                >拒绝</el-button>
                <el-button
                  v-if="row.user_id !== auth.user?.user_id"
                  link type="danger" size="small" @click="doRemoveUser(row as AuthUser)"
                >删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ═══ Tab 3:LLM 设置(按用户) ═══ -->
      <el-tab-pane label="LLM 设置" name="llm">
        <el-card shadow="never" class="llm-card">
          <div class="tab-hint llm-hint">
            配置你自己的大模型接口(OpenAI 兼容);保存后,你启动的写作任务将使用这份配置。
            留空则使用系统默认(MiniMax)。API Key 只保存在本机数据库,界面仅显示掩码。
          </div>
          <el-form label-position="top" class="llm-form">
            <el-form-item label="接口类型">
              <el-radio-group v-model="llmForm.llm_kind">
                <el-radio-button value="openai">OpenAI 兼容</el-radio-button>
                <el-radio-button value="anthropic">Anthropic 兼容</el-radio-button>
              </el-radio-group>
              <div class="tab-hint llm-kind-hint">{{ llmKindHint }}</div>
            </el-form-item>
            <el-form-item label="Base URL">
              <el-input v-model="llmForm.llm_base_url" :placeholder="llmBaseUrlPlaceholder" clearable />
            </el-form-item>
            <el-form-item label="API Key">
              <el-input
                v-model="llmForm.llm_api_key"
                type="password" show-password clearable
                :placeholder="auth.user?.llm_api_key_masked
                  ? `已保存 ${auth.user.llm_api_key_masked},留空表示不修改`
                  : '请输入 API Key'"
              />
            </el-form-item>
            <el-form-item label="模型">
              <div class="model-row">
                <el-select
                  v-model="llmForm.llm_model"
                  filterable allow-create default-first-option
                  placeholder="从上游获取后选择,或直接输入模型名"
                  class="model-select"
                >
                  <el-option
                    v-for="m in modelOptions"
                    :key="m.id"
                    :label="m.id"
                    :value="m.id"
                  />
                </el-select>
                <el-button :loading="llmProbing" @click="probeModels">从上游获取模型</el-button>
              </div>
            </el-form-item>
            <div class="llm-actions">
              <el-button type="primary" :loading="llmSaving" @click="saveLlm">保存配置</el-button>
              <span v-if="auth.user?.llm_base_url" class="llm-current">
                当前:{{ llmKindLabel }} · {{ auth.user.llm_base_url }} · {{ auth.user.llm_model || '(未选模型)' }}
              </span>
              <span v-else class="llm-current">当前:未配置,使用系统默认 MiniMax(OpenAI 兼容端点)</span>
            </div>
          </el-form>
        </el-card>
      </el-tab-pane>
    </el-tabs>

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
.admin-tabs {
  margin-top: -6px;
}
.tab-toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 14px;
}
.tab-hint {
  font-size: 12.5px;
  color: var(--ink-faint);
  line-height: 1.7;
}

/* ── LLM 设置 ── */
.llm-card {
  max-width: 720px;
}
.llm-hint {
  margin-bottom: 16px;
}
.llm-kind-hint {
  margin-top: 6px;
  line-height: 1.7;
}
.llm-form {
  max-width: 560px;
}
.model-row {
  display: flex;
  gap: 10px;
  width: 100%;
}
.model-select {
  flex: 1;
}
.llm-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 4px;
}
.llm-current {
  font-size: 12px;
  color: var(--ink-faint);
  word-break: break-all;
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
