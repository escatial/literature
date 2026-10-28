<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { storeToRefs } from 'pinia';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCollapse,
  ElCollapseItem,
  ElDivider,
  ElEmpty,
  ElInput,
  ElProgress,
  ElRadioButton,
  ElRadioGroup,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus';
import { toast } from '@/utils/toast';
import { http } from '@/api/http';
import { getTaskTopic } from '@/api/endpoints';
import { useTopicStore } from '@/stores/topic';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';
import { usePapersStore } from '@/stores/papers';
import { useWritingStore } from '@/stores/writing';
import type { Paper } from '@/api/types';

const topicStore = useTopicStore();
const ustore = useUnifiedRetrievalStore();
const papersStore = usePapersStore();
const writingStore = useWritingStore();

const form = reactive({
  mode: 'theme' as 'theme' | 'locale',
});

// 写作状态全局持有:切换 tab 不中断任务、不丢进度
const { stream, running, awaitingConfirm, topic, hasProgress } = storeToRefs(writingStore);

// 独立于 SSE 的本地计时：即使后端长时间没有业务事件，用户仍能看到任务真实运行时长。
const displayElapsedSeconds = ref(0);
let elapsedTimer: number | undefined;
let elapsedStartedAt = 0;
watch(running, (isRunning) => {
  if (isRunning) {
    elapsedStartedAt = Date.now();
    displayElapsedSeconds.value = 0;
    window.clearInterval(elapsedTimer);
    elapsedTimer = window.setInterval(() => {
      displayElapsedSeconds.value = Math.max(
        displayElapsedSeconds.value,
        Math.floor((Date.now() - elapsedStartedAt) / 1000),
      );
    }, 500);
  } else {
    window.clearInterval(elapsedTimer);
    elapsedTimer = undefined;
  }
}, { immediate: true });

const allPapers = ref<Paper[]>([]);
const loadingPapers = ref(false);
const activityLogEl = ref<HTMLElement | null>(null);

watch(() => stream.value.activityLog.length, async () => {
  await nextTick();
  if (activityLogEl.value) activityLogEl.value.scrollTop = 0;
});

// ============ Agent 执行过程:思考流 + 工具调用步骤 ============

const thinkingOpen = ref(true);
const thinkingEl = ref<HTMLElement | null>(null);

// 思考增量到达时贴底滚动;用户手动上翻后不打扰(距底 >40px 视为在回看)
watch(() => stream.value.currentReasoning.length, async () => {
  if (!thinkingOpen.value || !thinkingEl.value) return;
  const el = thinkingEl.value;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
  if (nearBottom || el.scrollHeight - el.clientHeight < 200) {
    await nextTick();
    el.scrollTop = el.scrollHeight;
  }
});

watch(thinkingOpen, async (open) => {
  if (open && thinkingEl.value) {
    await nextTick();
    thinkingEl.value.scrollTop = thinkingEl.value.scrollHeight;
  }
});

const stepDotClass = (status: 'running' | 'ok' | 'fail') =>
  status === 'running' ? 'dot-run' : status === 'ok' ? 'dot-ok' : 'dot-bad';

const loadPapers = async () => {
  loadingPapers.value = true;
  try {
    // 写作入口只加载文献池页勾选的集合；主题相关性与摘要质量由后端
    // 规划阶段统一筛选，前端不自行删减或改变文献归属。
    allPapers.value = await papersStore.fetchFiltered({
      selected_only: true,
      limit: 5000,
    });
    void fillTopicFromTask();
  } catch (e) {
    toast.error(`文献池加载失败:${String((e as Error)?.message ?? e)}`);
  } finally {
    loadingPapers.value = false;
  }
};

/** 从文献池导入文献后,研究主题为空时用该任务检索时的主题自动填充。
 *  主题来自后端按当前任务 id 关联的检索历史;已有主题(含用户手填)不覆盖。 */
const fillTopicFromTask = async () => {
  if (topicInput.value.trim() || allPapers.value.length === 0) return;
  try {
    const { topic } = await getTaskTopic();
    const t = topic.trim();
    if (t && !topicInput.value.trim()) {
      ustore.setTopic(t);
      topicStore.setTopic(t);
    }
  } catch {
    // 主题解析失败不影响写作页主流程
  }
};

onMounted(() => {
  void loadPapers();
});

onBeforeUnmount(() => {
  window.clearInterval(elapsedTimer);
});

const selectedPapers = computed(() => allPapers.value.filter((paper) => paper.selected));
const selectedCount = computed(() => selectedPapers.value.length);

// v11 写作池引导线(与后端 writing/pool_selection.py 同口径)。
// 口径对应:后端由 env WRITING_REF_MIN / WRITING_REF_MAX 配置
// (backend/src/writing/orchestrator.py _int_env,默认 70/90);
// 前端此处仅作展示阈值,后端改 env 时此处需同步调整。
const REFERENCE_LIMIT_MIN = 70;
const REFERENCE_LIMIT_MAX = 90;
const POOL_SOFT_CEILING = REFERENCE_LIMIT_MAX + 10;
const chineseSelectedCount = computed(() =>
  selectedPapers.value.filter((paper) => /^(cnki|user_imported)$/i.test(String(paper.source))).length,
);
const englishSelectedCount = computed(() => selectedCount.value - chineseSelectedCount.value);

// 相关性等级对应的标签配色
const gradeTagType = (grade: string) => {
    if (grade === 'high') return 'success';
    if (grade === 'low') return 'info';
    return 'warning';
};

// 配额裁剪层的可读标签(v11:核心期刊分层 × 相关性分级精选)
const quotaCutRows = computed(() => stream.value.relevanceReport?.quota_cut ?? []);
const coreTierLabel = (tier: string) => {
    if (tier === 'core') return '核心期刊';
    if (tier === 'non_core') return '非核心';
    return '英文';
};

const formatDuration = (seconds: number) => {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
};

const topicInput = computed({
  get: () => ustore.topic || topicStore.topic,
  set: (v: string) => {
    ustore.setTopic(v.trim());
    topicStore.setTopic(v.trim());
  },
});

const phaseLabel = computed(() => {
  // C7:阶段判定以事件 phase 为准(不再按活动日志文案匹配)。
  // writing 阶段尚未进入第一节 = 阶段2 刚启动的“准备中”窗口。
  if (stream.value.phase === 'writing' && !stream.value.progress) {
    return '章节写作准备中';
  }
  switch (stream.value.phase) {
    case 'start':
      return '初始化中';
    case 'screening': {
      const p = stream.value.screeningProgress;
      if (p) return `主题筛选中 (${p.processed}/${p.total})`;
      return '主题筛选中';
    }
    case 'classify':
      return '文献分组中';
    case 'await_confirm':
      return '等待确认主题划分';
    case 'writing': {
      const p = stream.value.progress;
      const title = stream.value.currentSection?.title;
      if (title && p) return `正在写《${title}》 (${p.index + 1}/${p.total})`;
      if (title) return `正在写《${title}》`;
      return '章节写作中';
    }
    case 'reference':
      return '整理参考文献中';
    case 'complete':
      return '已完成';
    case 'error':
      return '出错了';
    default:
      return '';
  }
});

const actionLabel = computed(() => {
  if (!running.value) {
    return awaitingConfirm.value ? '请在下方确认主题' : '① 划分主题';
  }
  // 阶段2由已确认的 plan 启动。即使某个过渡事件暂时仍把 phase 留在
  // classify，只要 plan 已存在且不再等待确认，按钮就必须显示正文状态。
  if (stream.value.phase === 'reference') return '整理参考文献中...';
  if (stream.value.phase === 'qa') return '质量核查中...';
  if (stream.value.activityLog.some((item) =>
    item.message.includes('主题已确认，开始生成综述正文'),
  )) {
    return '生成正文中...';
  }
  if (!awaitingConfirm.value && (
    stream.value.plan !== null ||
    stream.value.currentSection !== null ||
    stream.value.progress !== null
  )) {
    return '生成正文中...';
  }
  switch (stream.value.phase) {
    case 'screening': return '筛选文献中...';
    case 'classify': return '划分主题中...';
    case 'writing': return '生成正文中...';
    default: return '准备任务中...';
  }
});

const primaryActionDisabled = computed(() =>
  awaitingConfirm.value || !topicInput.value.trim() || selectedCount.value === 0 || loadingPapers.value,
);

const liveChars = computed(() => stream.value.currentSection?.content.length ?? 0);
const livePreview = computed(() => stream.value.currentSection?.content ?? '');

// ============ QA 软门禁报告展示(v9.7:FAIL 不再终止流,报告在此汇总) ============

interface QaIssueRow {
  code: string;
  severity: string;
  message: string;
  path?: string;
  lit_id?: string | null;
}

const qaOverallType = computed(() => {
  const overall = stream.value.qaResult?.overall;
  if (overall === 'pass') return 'success';
  if (overall === 'fail') return 'error';
  return 'warning';
});

const qaFailedIssues = computed<QaIssueRow[]>(() => {
  const issues = (stream.value.qaResult?.issues ?? []) as unknown as QaIssueRow[];
  return issues.filter((i) => i?.severity === 'fail');
});

const qaWarnIssues = computed<QaIssueRow[]>(() => {
  const issues = (stream.value.qaResult?.issues ?? []) as unknown as QaIssueRow[];
  return issues.filter((i) => i?.severity === 'warn');
});

const qaStatusTagType = (status: string) =>
  status === 'pass' ? 'success' : status === 'fail' ? 'danger' : status === 'warn' ? 'warning' : 'info';

// ============ 两阶段·阶段1:先划分主题,用户确认后才能写正文 ============

// ---------- 导出 Word ----------
const exportWord = async () => {
  // 后端生成带原生脚注的 docx(宋体小四/Times New Roman/1.5 倍行距)。
  // 必须走 axios blob:接口在鉴权中间件之后,window.open 新标签页
  // 带不上 X-Auth-Token(与文献池大文档下载同模式)。
  try {
    const resp = await http.get('/reviews/latest/export/docx', { responseType: 'blob' });
    const url = URL.createObjectURL(resp.data as Blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `文献综述-${topicInput.value.trim() || '导出'}.docx`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast.error(`导出失败:${String((e as Error)?.message ?? e)}`);
  }
};

const start = async () => {
  if (!topicInput.value.trim()) {
    toast.error('请先填写研究主题(可在本页直接编辑)');
    return;
  }
  if (selectedCount.value === 0) {
    toast.error('文献池为空,请先添加文献');
    return;
  }
  // 只跑阶段1(筛选+相关性分级+主题划分),到确认点即停,不写正文
  await writingStore.startPlan({
    topic: topicInput.value.trim(),
    papers: selectedPapers.value,
    classify_mode: form.mode,
    do_screening: true,
  });
};

// 确认面板的可编辑分组副本:确认前不改动 store 里的原始划分
interface EditableGroup { name: string; lit_ids: string[]; }
const editableGroups = ref<EditableGroup[]>([]);

// 到达确认点(含刷新恢复)时,用 store 的划分方案初始化编辑副本
watch(awaitingConfirm, (waiting) => {
  if (waiting) {
    editableGroups.value = (stream.value.plan?.groups ?? []).map((g) => ({ ...g }));
  }
}, { immediate: true });

// 阶段1筛选后文献池的 lit_id → 标题映射(用于组内文献预览)
const planPaperTitles = computed(() => {
  const map = new Map<string, string>();
  for (const p of stream.value.plan?.papers ?? []) {
    map.set(p.lit_id, p.title);
  }
  return map;
});

const groupPreview = (litIds: string[]) => {
  const titles = litIds.slice(0, 2).map((id) => planPaperTitles.value.get(id) ?? id);
  const rest = litIds.length - titles.length;
  const head = titles.join('；');
  return rest > 0 ? `${head} 等 ${litIds.length} 篇` : head;
};

const groupTitles = (litIds: string[]) => {
  const map = planPaperTitles.value;
  return litIds.slice(0, 6).map((id) => map.get(id) ?? id);
};

const blueprintGroup = (name: string) => blueprint.value?.organization.groups.find((g) => g.name === name);

const blueprint = computed(() => stream.value.plan?.blueprint ?? null);
const blueprintRows = computed(() => blueprint.value?.matrix ?? []);

const removeGroup = (idx: number) => {
  editableGroups.value.splice(idx, 1);
};

// 确认主题划分:只有用户点确认才进入阶段2正文写作
const confirmPlan = () => {
  const groups = editableGroups.value
    .map((g) => ({ name: g.name.trim(), lit_ids: g.lit_ids }))
    .filter((g) => g.name && g.lit_ids.length > 0);
  if (groups.length === 0) {
    toast.error('至少保留一个有效主题(组名不能为空,组内文献不能为空)');
    return;
  }
  writingStore.confirmGroups(groups);
};

// 对划分不满意:回到起点重新划分(清空编辑副本后重跑阶段1)
const rePlan = async () => {
  editableGroups.value = [];
  await start();
};

const stop = () => writingStore.stop();

const downloadMd = () => {
  const lines: string[] = [];
  lines.push(`# ${topicInput.value.trim()} 文献综述`, '');
  if (blueprint.value) {
    lines.push('## 综述设计卡', '');
    lines.push(`- 综述问题：${blueprint.value.research_question}`);
    lines.push(`- 组织方式：${blueprint.value.organization.mode}`);
    lines.push(`- 纳入文献：${blueprint.value.scope.included_count} 篇；筛除：${blueprint.value.scope.screened_out_count} 篇`);
    lines.push(`- 组织原则：${blueprint.value.organization.principle}`, '');
    lines.push('## 文献编码矩阵', '');
    lines.push('| 文献 | 年份 | 主题 | 方法线索 | 核心证据 | 局限 |', '| --- | --- | --- | --- | --- | --- |');
    for (const row of blueprint.value.matrix) {
      const cell = (v: unknown) => String(v ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ');
      lines.push(`| ${cell(row.title)} | ${cell(row.year)} | ${cell(row.group)} | ${cell(row.method)} | ${cell(row.core_findings)} | ${cell(row.limitation)} |`);
    }
    lines.push('');
  }
  for (const s of stream.value.sections) {
    lines.push(`## ${s.title}`, '', s.content, '');
  }
  lines.push('## 参考文献', '', stream.value.referenceList, '');
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `文献综述-${topicInput.value.trim()}.md`;
  a.click();
  URL.revokeObjectURL(url);
};
</script>

<template>
  <div class="writing-page">
    <!-- 后台运行提示:切 tab 写作不停,在顶部持续可见 -->
    <div v-if="running" class="run-banner">
      <span class="run-dot"></span>
      <div class="run-banner-main">
        <div class="run-banner-title">后台继续生成中 · {{ topic ?? '' }}</div>
        <div class="run-banner-sub">
          {{ phaseLabel }}<template v-if="stream.currentSection?.title"> · 当前章节:{{ stream.currentSection.title }}</template><template v-if="stream.currentSection?.content"> · 已输出 {{ stream.currentSection.content.length }} 字</template>
        </div>
      </div>
    </div>
    <el-alert
      v-else-if="hasProgress && !awaitingConfirm && stream.phase !== 'complete' && stream.phase !== 'error'"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 14px"
      title="检测到未完成的写作进度"
      description="上次生成意外中断。可点下方 清理进度 按钮清空。"
    >
      <template #default>
        <el-button size="small" type="primary" @click="writingStore.clearProgress()">清理进度</el-button>
      </template>
    </el-alert>

    <!-- 叁 · 综述写作:控制台 -->
    <el-card shadow="never" class="sheet">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-no">叁</span>
            <span class="section-title">综述写作</span>
          </div>
          <span v-if="!awaitingConfirm" class="section-note">流程:① 划分主题 → ② 您确认主题 → ③ 开始写作</span>
        </div>
      </template>

      <div class="bench">
        <div class="bench-row">
          <span class="bench-label">研究主题</span>
          <el-input
            v-model="topicInput"
            placeholder="写下综述的题目…"
            size="large"
            class="bench-topic"
            clearable
            :disabled="running"
          />
        </div>
        <div class="bench-row">
          <span class="bench-label">文献池</span>
          <span class="pool-pill"><b class="figure-num">{{ selectedCount }}</b> 篇待写</span>
          <span class="pool-pill zh">中文 <b class="figure-num">{{ chineseSelectedCount }}</b></span>
          <span class="pool-pill en">英文 <b class="figure-num">{{ englishSelectedCount }}</b></span>
          <span class="bench-hint">筛选后精选写作池至约 {{ REFERENCE_LIMIT_MAX }} 篇({{ POOL_SOFT_CEILING }} 篇以内不裁):中文核心期刊优先、非核心按相关性补足,英文按相关性取 30%-40%;裁剪明细见主题确认面板。</span>
        </div>
        <div class="bench-row">
          <span class="bench-label">章节划分</span>
          <el-radio-group v-model="form.mode" :disabled="running">
            <el-radio-button value="locale">按国内外</el-radio-button>
            <el-radio-button value="theme">按主题</el-radio-button>
          </el-radio-group>
          <span class="bench-hint">
            {{ form.mode === 'theme'
              ? '依据全部文献摘要聚类归纳实际研究主题,每个主题写一节'
              : '按中文/外文两节分述,国内一节(知网)+ 国外一节(OpenAlex/PubMed)' }}
          </span>
        </div>
      </div>

      <div class="launch-row">
        <el-button
          type="primary"
          size="large"
          class="launch-btn"
          :loading="running"
          :disabled="primaryActionDisabled"
          @click="start"
        >
          {{ actionLabel }}
        </el-button>
        <el-button v-if="running" type="danger" size="large" plain @click="stop">
          停止任务
        </el-button>
        <el-button
          v-if="stream.phase === 'error' && !running"
          type="warning"
          size="large"
          plain
          @click="rePlan"
        >
          重新划分主题
        </el-button>
        <el-button
          v-if="stream.phase === 'complete'"
          type="success"
          size="large"
          plain
          @click="exportWord"
        >
          导出 Word(含脚注)
        </el-button>
      </div>

      <div v-if="running || stream.phase !== 'idle'" class="monitor">
        <div class="monitor-line">
          <span class="phase-chip" :class="stream.phase === 'error' ? 'st-bad' : 'st-run'">{{ phaseLabel }}</span>
          <span v-if="stream.currentSection?.title && stream.phase === 'writing'" class="phase-chip st-wait">
            {{ stream.currentSection.title }}
          </span>
          <span v-if="stream.elapsedSeconds > 0" class="phase-chip st-idle">
            已运行 {{ formatDuration(Math.max(displayElapsedSeconds, stream.elapsedSeconds)) }}
          </span>
          <span
            v-if="running && stream.waitingSeconds >= 2"
            class="phase-chip st-warn"
          >
            当前阶段无新结果 {{ stream.waitingSeconds }} 秒
          </span>
          <span v-if="stream.phase === 'screening' && stream.screeningProgress" class="phase-chip st-ok">
            已处理 {{ stream.screeningProgress.processed }}/{{ stream.screeningProgress.total }} 篇
          </span>
        </div>
        <el-progress
          v-if="stream.phase === 'screening' && stream.screeningProgress"
          :percentage="Math.round((stream.screeningProgress.processed / Math.max(stream.screeningProgress.total, 1)) * 100)"
          :indeterminate="stream.screeningProgress.status === 'started' && stream.screeningProgress.batch === 0"
          :duration="2"
          :format="() => stream.screeningProgress!.batch === 0
            ? `准备筛选，共 ${stream.screeningProgress!.total} 篇`
            : `${stream.screeningProgress!.processed}/${stream.screeningProgress!.total} 篇 · 第 ${stream.screeningProgress!.batch}/${stream.screeningProgress!.totalBatches} 批`"
          style="margin-top: 10px"
        />
        <el-alert
          v-if="running && stream.phase === 'screening'"
          type="info"
          :closable="false"
          show-icon
          style="margin-top: 10px"
          :title="stream.screeningProgress?.batch
            ? `筛选第 ${stream.screeningProgress.batch}/${stream.screeningProgress.totalBatches} 批 · 已处理 ${stream.screeningProgress.processed}/${stream.screeningProgress.total} 篇`
            : '筛选任务已启动，正在准备第一批文献'"
          :description="stream.waitingSeconds >= 2
            ? `模型正在分析当前批次，已等待 ${stream.waitingSeconds} 秒；任务仍在运行，请勿重复点击。`
            : '每批完成后会立即更新进度，筛选完成后才会进入文献分组。'"
        />
        <el-progress
          v-if="stream.progress"
          :percentage="stream.phase === 'complete' ? 100 : Math.round((stream.progress.index / stream.progress.total) * 100)"
          :indeterminate="stream.phase === 'writing'"
          :duration="2"
          :format="() => `${Math.min(stream.progress!.index + 1, stream.progress!.total)}/${stream.progress!.total} 章`"
          style="margin-top: 10px"
        />
        <div v-if="stream.detail" class="monitor-detail">
          {{ stream.detail }}
        </div>
        <div v-if="stream.harness" class="monitor-harness">
          Agent 状态：{{ stream.harness.stage || '初始化' }} ·
          {{ stream.harness.status || 'running' }} ·
          第 {{ stream.harness.attempt || 0 }} 轮
          <span v-if="stream.harness.repairs?.length"> · 已修复 {{ stream.harness.repairs.length }} 次</span>
        </div>
        <!-- Agent 执行过程:工具调用步骤(对应后端 section_step 事件) -->
        <div v-if="stream.sectionSteps.length" class="agent-steps">
          <div class="agent-steps-title">Agent 执行过程</div>
          <div
            v-for="s in stream.sectionSteps"
            :key="s.id"
            class="agent-step"
          >
            <span class="step-dot" :class="stepDotClass(s.status)"></span>
            <span class="step-label" :class="{ 'step-fail': s.status === 'fail' }">{{ s.label }}</span>
            <span v-if="s.seconds != null" class="step-seconds">{{ s.seconds }}s</span>
            <span class="step-at">{{ s.at }}</span>
          </div>
        </div>
        <!-- 思考过程:模型 reasoning 流式增量,只上屏不入正文 -->
        <div v-if="stream.currentReasoning" class="thinking">
          <button type="button" class="thinking-head" @click="thinkingOpen = !thinkingOpen">
            <span class="thinking-title">思考过程</span>
            <span class="thinking-meta">
              {{ thinkingOpen ? '收起' : '展开' }} · 约 {{ stream.currentReasoning.length }} 字
            </span>
          </button>
          <div v-show="thinkingOpen" ref="thinkingEl" class="thinking-body">{{ stream.currentReasoning }}</div>
        </div>
        <el-alert
          v-if="stream.phase === 'error'"
          type="error"
          :closable="false"
          show-icon
          style="margin-top: 10px"
          title="主题划分未完成"
          :description="stream.error
            ? `${stream.error}。请点击“重新划分主题”重试；已完成的文献池不会被删除。`
            : '本次模型返回的主题结果不完整，系统没有生成伪主题。请点击“重新划分主题”重试；已完成的文献池不会被删除。'"
        />
        <el-collapse v-if="stream.activityLog.length" class="activity">
          <el-collapse-item name="activity" title="实时工作记录">
            <div ref="activityLogEl" class="activity-log">
              <div v-for="item in stream.activityLog.slice().reverse()" :key="item.id" class="activity-line">
                <span class="activity-at">{{ item.at }}</span>{{ item.message }}
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
        <div
          v-if="stream.phase === 'writing' && livePreview"
          class="live"
        >
          <div class="live-title">
            正文流式输出<template v-if="stream.currentSection?.title"> · {{ stream.currentSection.title }}</template>
          </div>
          <div class="live-paper">{{ livePreview }}</div>
        </div>
      </div>
    </el-card>

    <!-- 两阶段·确认点:主题划分完成,必须由用户确认后才进入正文写作 -->
    <template v-if="awaitingConfirm">
      <el-card v-if="blueprint" shadow="never" class="sheet" style="margin-top: 18px">
        <template #header>
          <div class="section-head">
            <div class="head-main">
              <span class="section-no">案</span>
              <span class="section-title">综述设计卡与证据矩阵</span>
            </div>
            <span class="section-note">
              {{ blueprint.organization.mode }} · 纳入 {{ blueprint.scope.included_count }} 篇 · 筛除 {{ blueprint.scope.screened_out_count }} 篇<template v-if="blueprint.scope.quota_cut_count"> · 配额裁剪 {{ blueprint.scope.quota_cut_count }} 篇</template>
            </span>
          </div>
        </template>
        <div class="blueprint-facts">
          <div><b>综述问题</b>{{ blueprint.research_question }}</div>
          <div><b>范围</b>{{ blueprint.scope.time_range[0] ?? '未定' }} — {{ blueprint.scope.time_range[1] ?? '未定' }},数据源 {{ blueprint.scope.sources.join('、') || '未标注' }}</div>
          <div><b>纳入标准</b>{{ blueprint.inclusion_criteria.join('；') }}</div>
          <div><b>排除标准</b>{{ blueprint.exclusion_criteria.join('；') }}</div>
          <div><b>组织原则</b>{{ blueprint.organization.principle }}</div>
        </div>
        <el-table :data="blueprintRows" size="small" border stripe max-height="420" style="margin-top: 14px">
          <el-table-column prop="title" label="文献" min-width="260" show-overflow-tooltip />
          <el-table-column prop="year" label="年份" width="72" />
          <el-table-column prop="group" label="主题" width="140" show-overflow-tooltip />
          <el-table-column prop="method" label="方法线索" width="150" show-overflow-tooltip />
          <el-table-column prop="data_or_sample" label="数据/样本" width="130" show-overflow-tooltip />
          <el-table-column prop="core_findings" label="摘要证据" min-width="280" show-overflow-tooltip />
          <el-table-column prop="limitation" label="局限与缺口" width="140" show-overflow-tooltip />
        </el-table>
        <div class="matrix-note">
          矩阵中的“待编码”表示元数据不足，需在精读阶段补全；系统不会用常识替代原文证据。
        </div>
      </el-card>
      <el-alert
        :type="stream.plan?.classify_fallback ? 'error' : 'warning'"
        :closable="false"
        show-icon
        style="margin-top: 18px"
        :title="stream.plan?.classify_fallback
          ? '自动主题划分失败,当前仅 1 个兜底主题 — 强烈建议重新划分'
          : '主题划分完成,请确认后才会开始写作'"
        :description="stream.plan?.classify_fallback
          ? '模型输出未能可靠完成主题划分，系统已自动整理候选主题。请检查下方主题及文献归属；不满意可重新划分。确认前不会生成正文。'
          : '确认前不会生成任何正文。可修改主题名称、删除不需要的主题;确认后将按这些主题分节写作。'"
      />
      <el-card shadow="never" class="sheet" style="margin-top: 14px">
        <template #header>
          <div class="section-head">
            <div class="head-main">
              <span class="section-no">②</span>
              <span class="section-title">主题划分确认</span>
            </div>
            <span class="section-note">
              共 {{ editableGroups.length }} 个主题 · 覆盖 {{ editableGroups.reduce((n, g) => n + g.lit_ids.length, 0) }} 篇文献
            </span>
          </div>
        </template>
        <div
          v-for="(g, idx) in editableGroups"
          :key="idx"
          class="group-row"
        >
          <span class="group-no">主题 {{ idx + 1 }}</span>
          <el-input
            v-model="g.name"
            placeholder="主题名称"
            :disabled="running"
            class="group-name"
            :title="g.name"
          />
          <span class="group-count"><b class="figure-num">{{ g.lit_ids.length }}</b> 篇</span>
          <span :title="groupPreview(g.lit_ids)" class="group-preview">
            {{ groupPreview(g.lit_ids) }}
          </span>
          <el-button
            type="danger"
            plain
            size="small"
            :disabled="running"
            @click="removeGroup(idx)"
          >
            删除主题
          </el-button>
          <div v-if="blueprintGroup(g.name)" class="group-meta">
            <span v-if="blueprintGroup(g.name)?.boundary">边界：{{ blueprintGroup(g.name)?.boundary }}</span>
            <span v-if="blueprintGroup(g.name)?.relation_hint">比较：{{ blueprintGroup(g.name)?.relation_hint }}</span>
          </div>
        </div>
        <el-collapse class="group-detail">
          <el-collapse-item
            v-for="(g, idx) in editableGroups"
            :key="`detail-${idx}`"
            :name="`detail-${idx}`"
            :title="`${g.name || `主题 ${idx + 1}`} · ${g.lit_ids.length} 篇文献`"
          >
            <div class="group-titles">
              <div v-for="title in groupTitles(g.lit_ids)" :key="title">{{ title }}</div>
              <span v-if="g.lit_ids.length > 6" class="group-more">其余 {{ g.lit_ids.length - 6 }} 篇已折叠</span>
            </div>
          </el-collapse-item>
        </el-collapse>
        <el-empty
          v-if="editableGroups.length === 0"
          description="已无主题:全部删除将无法写作,请重新划分"
          :image-size="60"
        />
        <div class="confirm-row">
          <el-button type="primary" size="large" :loading="running" @click="confirmPlan">
            确认主题,开始写作
          </el-button>
          <el-button size="large" :disabled="running" @click="rePlan">
            不满意,重新划分
          </el-button>
        </div>
      </el-card>
    </template>

    <el-collapse v-if="stream.screenedOutIds.length" style="margin-top: 18px">
      <el-collapse-item :title="`筛选剔除 · ${stream.screenedOutIds.length} 篇(点击展开明细)`" name="screened">
        <span class="screened-list">
          {{ stream.screenedOutIds.join(', ') }}
        </span>
      </el-collapse-item>
    </el-collapse>
    <el-alert
      v-if="stream.droppedCitations.length"
      type="error"
      :closable="false"
      style="margin-top: 10px"
      title="检测到幻觉引用已剥离"
    >
      {{ stream.droppedCitations.join(', ') }}
    </el-alert>

    <el-card v-if="stream.relevanceReport" shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-title">文献相关性分级清单</span>
          </div>
          <span class="section-note">
            共 {{ stream.relevanceReport.total }} 篇 ·
            高相关 {{ stream.relevanceReport.summary.high }} ·
            中相关 {{ stream.relevanceReport.summary.medium }} ·
            低相关 {{ stream.relevanceReport.summary.low }}
          </span>
        </div>
      </template>
      <div class="relevance-criteria">
        高相关标准（须同时满足）：核心关键词重合度 ≥
        {{ stream.relevanceReport.criteria.keyword_overlap_high }}%、研究领域完全贴合（≥
        {{ stream.relevanceReport.criteria.field_match_full }}）、研究方法可直接参考（≥
        {{ stream.relevanceReport.criteria.method_applicable }}）。低相关文献仅作背景补充，不得作为核心论据。
      </div>
      <el-table :data="stream.relevanceReport.items" size="small" max-height="420" border>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-facts">
              <div>核心匹配点：{{ row.match_points.join('；') }}</div>
              <div v-if="row.reason">筛选说明：{{ row.reason }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="lit_id" label="文献 ID" width="150" />
        <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
        <el-table-column label="等级" width="90">
          <template #default="{ row }">
            <el-tag :type="gradeTagType(row.grade)" effect="plain" size="small">
              {{ row.grade_label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total" label="总分" width="80" />
        <el-table-column label="关键词重合" width="100">
          <template #default="{ row }">{{ row.dimensions.keyword_overlap }}%</template>
        </el-table-column>
        <el-table-column label="领域匹配" width="90">
          <template #default="{ row }">{{ row.dimensions.field_match }}</template>
        </el-table-column>
        <el-table-column label="方法适用" width="90">
          <template #default="{ row }">{{ row.dimensions.method_applicability }}</template>
        </el-table-column>
        <el-table-column label="结论价值" width="90">
          <template #default="{ row }">{{ row.dimensions.conclusion_value }}</template>
        </el-table-column>
      </el-table>
      <el-alert
        v-if="stream.relevanceReport.excluded_low_relevance.length"
        type="info"
        :closable="false"
        style="margin-top: 12px"
        title="已排除出引用配额的低相关文献"
      >
        {{ stream.relevanceReport.excluded_low_relevance.length }} 篇：{{
          stream.relevanceReport.excluded_low_relevance.map((r) => r.lit_id).join(', ')
        }}
      </el-alert>
      <el-collapse v-if="quotaCutRows.length" style="margin-top: 12px">
        <el-collapse-item
          :title="`写作池配额裁剪 · ${quotaCutRows.length} 篇(相关性合格但超出写作池总量,点击展开明细)`"
          name="quota_cut"
        >
          <el-table :data="quotaCutRows" size="small" max-height="320" border>
            <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
            <el-table-column prop="journal" label="期刊" min-width="140" show-overflow-tooltip />
            <el-table-column prop="year" label="年份" width="72" />
            <el-table-column label="层级" width="96">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" :type="row.core_tier === 'core' ? 'success' : 'info'">
                  {{ coreTierLabel(row.core_tier) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="grade" label="相关等级" width="90" />
            <el-table-column prop="reason" label="裁剪原因" min-width="220" show-overflow-tooltip />
          </el-table>
          <div class="relevance-criteria" style="margin-top: 8px">
            裁剪规则:中文核心期刊优先,同层按相关性等级与总分排序;非核心文献在核心不足配额时按同一顺序补足;
            英文按相关性排序、同分时被引数高者优先,占比约为写作池的 30%-40%。被裁文献不影响检索池,可随时重新划分。
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-card v-if="stream.groups.length && !awaitingConfirm" shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-title">文献分组</span>
          </div>
        </div>
      </template>
      <div class="group-strip">
        <div v-for="g in stream.groups" :key="g.name" class="group-strip-item">
          <span class="group-strip-name" :title="g.name">{{ g.name }}</span>
          <span class="group-strip-count">{{ g.lit_ids.length }} 篇</span>
        </div>
      </div>
    </el-card>

    <!-- 成稿:手稿纸 -->
    <el-card v-if="stream.sections.length" shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-no">成</span>
            <span class="section-title manuscript-heading">{{ topicInput.trim() || '文献综述' }} 文献综述</span>
          </div>
          <el-button
            type="primary"
            plain
            :disabled="stream.phase !== 'complete'"
            @click="downloadMd"
          >
            下载 Markdown
          </el-button>
        </div>
      </template>

      <div class="manuscript">
        <template v-for="(s, i) in stream.sections" :key="s.key">
          <h3 class="manuscript-h3">
            <span class="manuscript-no">{{ i + 1 }}</span>{{ s.title }}
            <span v-if="s.citations.length" class="cite-chip">{{ s.citations.length }} 引用</span>
          </h3>
          <p class="manuscript-body">{{ s.content }}</p>
        </template>

        <hr class="manuscript-rule" />
        <h3 class="manuscript-h3"><span class="manuscript-no">附</span>参考文献</h3>
        <div class="manuscript-refs">{{ stream.referenceList || '(等待生成...)' }}</div>
      </div>
    </el-card>

    <!-- v9.7 软门禁:核查未通过不再报废综述,报告在此醒目展示供人工复核 -->
    <el-card v-if="stream.qaResult && stream.phase === 'complete'" shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-no">检</span>
            <span class="section-title">全流程质量核查报告</span>
          </div>
          <span class="section-note">
            通过率 <b class="figure-num">{{ Math.round((stream.qaResult.pass_rate ?? 0) * 100) }}%</b>
          </span>
        </div>
      </template>
      <el-alert
        :type="qaOverallType"
        :closable="false"
        show-icon
        :title="stream.qaResult.overall === 'pass'
          ? '四项核查全部通过'
          : stream.qaResult.overall === 'fail'
            ? '核查未通过 — 综述仍已生成,请结合下列问题人工复核'
            : '核查存在警告,建议复核'"
      />
      <div class="qa-chips">
        <span
          v-for="c in stream.qaResult.checks"
          :key="c.check_id"
          class="qa-chip"
          :class="`qa-${c.status}`"
        >
          {{ c.name }}:{{ c.status === 'pass' ? '通过' : c.status === 'fail' ? `${c.fail_count} 项不通过` : c.status === 'warn' ? `${c.warn_count} 项警告` : '跳过' }}
        </span>
      </div>
      <el-collapse v-if="qaFailedIssues.length || qaWarnIssues.length" class="qa-issues">
        <el-collapse-item
          v-if="qaFailedIssues.length"
          :title="`不通过的问题(${qaFailedIssues.length},点击展开)`"
        >
          <div
            v-for="(issue, i) in qaFailedIssues.slice(0, 30)"
            :key="i"
            class="issue-line issue-fail"
          >
            [{{ issue.code }}] {{ issue.message }}{{ issue.path ? `(${issue.path})` : '' }}
          </div>
          <div v-if="qaFailedIssues.length > 30" class="issue-rest">
            其余 {{ qaFailedIssues.length - 30 }} 条从略,完整报告见归档记录。
          </div>
        </el-collapse-item>
        <el-collapse-item
          v-if="qaWarnIssues.length"
          :title="`警告(${qaWarnIssues.length},点击展开)`"
        >
          <div
            v-for="(issue, i) in qaWarnIssues.slice(0, 30)"
            :key="i"
            class="issue-line issue-warn"
          >
            [{{ issue.code }}] {{ issue.message }}{{ issue.path ? `(${issue.path})` : '' }}
          </div>
          <div v-if="qaWarnIssues.length > 30" class="issue-rest">
            其余 {{ qaWarnIssues.length - 30 }} 条从略。
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-empty v-else-if="!running" description="尚未生成综述" style="margin-top: 32px" />
  </div>
</template>

<style scoped>
.writing-page {
  display: block;
}

/* ── 后台运行横幅 ── */
.run-banner {
  display: flex;
  align-items: center;
  gap: 11px;
  margin-bottom: 14px;
  padding: 11px 16px;
  background: var(--sheet);
  border: 1px solid var(--rule);
  border-left: 4px solid var(--cinnabar);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sheet);
}
.run-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--cinnabar);
  animation: run-pulse 1.4s ease-in-out infinite;
  flex: none;
}
@keyframes run-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
.run-banner-main {
  min-width: 0;
}
.run-banner-title {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.04em;
}
.run-banner-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ink-soft);
}

/* ── 写作台 ── */
.bench {
  display: grid;
  gap: 12px;
  padding-bottom: 16px;
  border-bottom: 1px dashed var(--rule-soft);
}
.bench-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.bench-label {
  font-family: var(--font-display);
  font-size: 13.5px;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: 0.12em;
  min-width: 66px;
  flex: none;
}
.bench-topic {
  flex: 1;
  min-width: 320px;
  max-width: 560px;
}
.pool-pill {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  font-size: 12.5px;
  color: var(--ink-soft);
  padding: 3px 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-sm);
  background: var(--sheet-dim);
}
.pool-pill .figure-num {
  font-size: 16px;
  color: var(--ink);
}
.pool-pill.zh {
  border-color: var(--zh-line);
  background: var(--zh-bg);
}
.pool-pill.zh .figure-num {
  color: var(--zh);
}
.pool-pill.en {
  border-color: var(--en-line);
  background: var(--en-bg);
}
.pool-pill.en .figure-num {
  color: var(--en);
}
.bench-hint {
  font-family: var(--font-annotate);
  font-size: 12px;
  color: var(--ink-faint);
  line-height: 1.6;
}

.launch-row {
  margin-top: 16px;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.launch-btn {
  letter-spacing: 0.18em;
}

/* ── 运行监视 ── */
.monitor {
  margin-top: 16px;
}
.monitor-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.phase-chip {
  display: inline-block;
  font-size: 12px;
  letter-spacing: 0.04em;
  padding: 3px 11px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}
.st-ok { color: var(--ok); background: var(--ok-bg); border-color: var(--ok); }
.st-warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
.st-bad { color: var(--bad); background: var(--bad-bg); border-color: var(--bad); }
.st-run { color: var(--cinnabar); background: var(--cinnabar-tint); border-color: var(--cinnabar); }
.st-wait { color: var(--ink-soft); background: var(--paper-deep); border-color: var(--rule-strong); }
.st-idle { color: var(--ink-soft); background: transparent; border-color: var(--rule); }

.monitor-detail {
  margin-top: 10px;
  padding: 10px 14px;
  border-left: 3px solid var(--rule-strong);
  background: var(--sheet-dim);
  color: var(--ink-soft);
  line-height: 1.75;
  font-size: 12.5px;
}
.monitor-harness {
  margin-top: 8px;
  color: var(--ink-faint);
  font-size: 12px;
  line-height: 1.7;
  font-family: var(--font-mono);
}

/* ── Agent 执行过程:步骤流水 + 思考流 ── */
.agent-steps {
  margin-top: 10px;
  padding: 10px 14px;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--sheet-dim);
}
.agent-steps-title {
  font-family: var(--font-display);
  font-size: 12.5px;
  letter-spacing: 0.08em;
  color: var(--ink-soft);
  margin-bottom: 6px;
}
.agent-step {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--ink-soft);
}
.step-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  align-self: center;
}
.dot-run {
  background: var(--cinnabar);
  animation: step-pulse 1.2s ease-in-out infinite;
}
.dot-ok { background: var(--ok); }
.dot-bad { background: var(--bad); }
@keyframes step-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.step-label { flex: 1 1 auto; min-width: 0; }
.step-fail { color: var(--bad); }
.step-seconds {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-faint);
}
.step-at {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-ghost);
}

.thinking {
  margin-top: 10px;
  border: 1px dashed var(--rule-strong);
  border-radius: var(--radius);
  background: var(--paper-deep);
}
.thinking-head {
  display: flex;
  width: 100%;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 14px;
  border: none;
  background: transparent;
  cursor: pointer;
  font: inherit;
}
.thinking-title {
  font-family: var(--font-display);
  font-size: 12.5px;
  letter-spacing: 0.08em;
  color: var(--ink-soft);
}
.thinking-meta {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-ghost);
}
.thinking-body {
  max-height: 220px;
  overflow-y: auto;
  margin: 0 14px 10px;
  padding: 10px 12px;
  background: #fffef9;
  border: 1px solid var(--rule-soft);
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.8;
  color: var(--ink-faint);
  font-style: italic;
  white-space: pre-wrap;
  word-break: break-word;
}

.activity {
  margin-top: 10px;
}
.activity-log {
  max-height: 180px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.9;
  color: var(--ink-soft);
}
.activity-at {
  color: var(--ink-ghost);
  margin-right: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
}

/* 流式输出:铺一张小稿纸 */
.live {
  margin-top: 12px;
}
.live-title {
  margin-bottom: 7px;
  color: var(--ink-soft);
  font-size: 13px;
  font-family: var(--font-display);
  letter-spacing: 0.05em;
}
.live-paper {
  max-height: 320px;
  min-height: 160px;
  overflow-y: auto;
  line-height: 1.9;
  color: var(--ink);
  font-size: 13.5px;
  padding: 14px 18px;
  background: #fffef9;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  white-space: pre-wrap;
  text-align: justify;
}

/* ── 设计卡 ── */
.blueprint-facts {
  display: grid;
  gap: 7px;
  color: var(--ink-soft);
  line-height: 1.75;
  font-size: 13px;
}
.blueprint-facts b {
  font-family: var(--font-display);
  color: var(--ink);
  margin-right: 10px;
  letter-spacing: 0.08em;
}
.blueprint-facts b::after {
  content: '　';
}
.matrix-note {
  margin-top: 10px;
  font-family: var(--font-annotate);
  color: var(--ink-faint);
  font-size: 12px;
}

/* ── 主题确认 ── */
.group-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px dashed var(--rule-soft);
  flex-wrap: wrap;
}
.group-no {
  color: var(--ink-ghost);
  font-size: 12px;
  min-width: 56px;
  flex: none;
  font-family: var(--font-display);
  letter-spacing: 0.08em;
}
.group-name {
  width: 320px;
  max-width: 100%;
}
.group-count {
  font-size: 12.5px;
  color: var(--ink-soft);
  flex: none;
}
.group-count .figure-num {
  font-size: 15px;
  color: var(--cinnabar);
}
.group-preview {
  flex: 1;
  min-width: 200px;
  color: var(--ink-faint);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-meta {
  width: 100%;
  padding: 4px 0 2px 68px;
  color: var(--ink-faint);
  font-size: 12px;
  line-height: 1.7;
  font-family: var(--font-annotate);
}
.group-detail {
  margin-top: 10px;
}
.group-titles {
  display: grid;
  gap: 5px;
  color: var(--ink-soft);
  font-size: 12px;
}
.group-titles > div {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.group-more {
  color: var(--ink-ghost);
}
.confirm-row {
  margin-top: 16px;
  display: flex;
  gap: 10px;
}

.screened-list {
  color: var(--ink-faint);
  font-size: 12px;
  line-height: 1.8;
  word-break: break-all;
}

/* ── 相关性清单 ── */
.relevance-criteria {
  margin-bottom: 12px;
  font-family: var(--font-annotate);
  color: var(--ink-faint);
  font-size: 12.5px;
  line-height: 1.75;
}
.expand-facts {
  padding: 8px 16px;
  color: var(--ink-soft);
  line-height: 1.9;
  font-size: 13px;
}

/* ── 分组条 ── */
.group-strip {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.group-strip-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 2px;
  border-bottom: 1px dashed var(--rule-soft);
}
.group-strip-item:last-child {
  border-bottom: none;
}
.group-strip-name {
  font-size: 13.5px;
  color: var(--ink);
  font-weight: 600;
}
.group-strip-count {
  font-size: 12px;
  color: var(--ink-faint);
  flex: none;
}

/* ── 手稿 ── */
.manuscript-heading {
  font-size: 19px;
}
.manuscript {
  max-width: 820px;
  margin: 0 auto;
  padding: 8px 4px 4px;
}
.manuscript-h3 {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.05em;
  margin: 26px 0 12px;
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.manuscript-h3:first-child {
  margin-top: 6px;
}
.manuscript-no {
  color: var(--cinnabar);
  font-size: 13px;
  letter-spacing: 0.1em;
  flex: none;
}
.cite-chip {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  color: var(--info);
  background: var(--info-bg);
  border: 1px solid var(--info);
  border-radius: var(--radius-sm);
  padding: 1px 7px;
  letter-spacing: 0.04em;
}
.manuscript-body {
  margin: 0;
  font-size: 14px;
  line-height: 2;
  color: var(--ink);
  text-align: justify;
  text-indent: 2em;
  white-space: pre-wrap;
}
.manuscript-rule {
  border: none;
  border-top: 1px solid var(--rule-strong);
  margin: 30px 0 0;
}
.manuscript-refs {
  margin-top: 12px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--ink-soft);
  white-space: pre-wrap;
}

/* ── QA 报告 ── */
.qa-chips {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.qa-chip {
  display: inline-block;
  font-size: 12px;
  padding: 3px 11px;
  border-radius: var(--radius-sm);
  border: 1px solid;
  letter-spacing: 0.02em;
}
.qa-pass { color: var(--ok); background: var(--ok-bg); border-color: var(--ok); }
.qa-fail { color: var(--bad); background: var(--bad-bg); border-color: var(--bad); }
.qa-warn { color: var(--warn); background: var(--warn-bg); border-color: var(--warn); }
.qa-skip { color: var(--ink-soft); background: transparent; border-color: var(--rule); }

.qa-issues {
  margin-top: 12px;
}
.issue-line {
  font-size: 13px;
  line-height: 1.85;
}
.issue-fail { color: var(--bad); }
.issue-warn { color: var(--warn); }
.issue-rest {
  font-size: 12px;
  color: var(--ink-ghost);
}
</style>
