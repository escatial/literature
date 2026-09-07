<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { storeToRefs } from 'pinia';
import {
  ElAlert,
  ElButton,
  ElCard,
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

// 组件挂载时尝试恢复快照(刷新页面或上次崩溃也能恢复进度)
writingStore.restore();

const allPapers = ref<Paper[]>([]);
const loadingPapers = ref(false);

const loadPapers = async () => {
  loadingPapers.value = true;
  try {
    // 写作池 = 文献池页「去写作」筛选导入的集合(selected=true),此处不再做二次筛选
    allPapers.value = await papersStore.fetchFiltered({
      selected_only: true,
      limit: 5000,
    });
  } catch (e) {
    toast.error(`文献池加载失败:${String((e as Error)?.message ?? e)}`);
  } finally {
    loadingPapers.value = false;
  }
};

onMounted(loadPapers);

const selectedPapers = computed(() => allPapers.value.filter((paper) => paper.selected));
const selectedCount = computed(() => selectedPapers.value.length);

const REFERENCE_LIMIT_MIN = 70;
const REFERENCE_LIMIT_MAX = 90;
const CHINESE_RATIO_MIN = 2 / 3;
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
  <div>
    <!-- 后台运行提示:切 tab 写作不停,在顶部持续可见 -->
    <el-alert
      v-if="running"
      type="success"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      :title="`后台继续生成中 · 主题《${topic ?? ''}》`"
      :description="phaseLabel + (stream.currentSection?.title ? ' · 当前章节:' + stream.currentSection.title : '') + (stream.currentSection?.content ? ' · 已输出 ' + stream.currentSection.content.length + ' 字' : '')"
    />
    <el-alert
      v-else-if="hasProgress && !awaitingConfirm && stream.phase !== 'complete' && stream.phase !== 'error'"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      title="检测到未完成的写作进度"
      description="上次生成意外中断。可点下方 清理进度 按钮清空。"
    >
      <template #default>
        <el-button size="small" type="primary" @click="writingStore.clearProgress()">清理进度</el-button>
      </template>
    </el-alert>

    <el-card>
      <template #header>综述写作</template>
      <div style="margin-bottom: 12px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px">
        <span style="color: #606266">研究主题</span>
        <el-input
          v-model="topicInput"
          placeholder="研究主题"
          size="large"
          clearable
          :disabled="running"
          style="width: 420px; max-width: 100%"
        />
        <span style="color: #606266">文献池</span>
        <el-tag type="success" size="large">{{ selectedCount }} 篇</el-tag>
        <el-tag size="large" type="warning" effect="plain">
          中文 {{ chineseSelectedCount }}
        </el-tag>
        <el-tag size="large" type="info" effect="plain">
          英文 {{ englishSelectedCount }}
        </el-tag>
        <span style="color: #909399; font-size: 12px">
          综述引用 {{ REFERENCE_LIMIT_MIN }}-{{ REFERENCE_LIMIT_MAX }} 篇，中文占比不低于 {{ Math.round(CHINESE_RATIO_MIN * 100) }}%；中文不足时会自动补充。
        </span>
      </div>
      <div style="margin-bottom: 12px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px">
        <span style="color: #606266">章节分类方式</span>
        <el-radio-group v-model="form.mode" :disabled="running">
          <el-radio-button value="locale">按国内外</el-radio-button>
          <el-radio-button value="theme">按主题</el-radio-button>
        </el-radio-group>
      </div>
      <div style="margin-bottom: 12px; color: #909399; font-size: 12px">
        {{ form.mode === 'theme'
          ? 'LLM 动态归纳 3-5 个并列研究主题,每个主题写一节'
          : '按中文/外文两节分述,国内一节(知网)+ 国外一节(OpenAlex/PubMed)' }}
        <el-tag v-if="!awaitingConfirm" type="warning" effect="plain" size="small" style="margin-left: 8px">
          流程:① 划分主题 → ② 您确认主题 → ③ 开始写作
        </el-tag>
      </div>
      <el-button
        type="primary"
        size="large"
        :loading="running"
        :disabled="!topicInput.trim() || selectedCount === 0 || loadingPapers"
        @click="start"
      >
        {{ running ? '划分主题中...' : (awaitingConfirm ? '重新划分主题' : '① 划分主题') }}
      </el-button>
      <el-button v-if="running" type="danger" size="large" plain @click="stop">
        停止生成
      </el-button>

      <div v-if="running || stream.phase !== 'idle'" style="margin-top: 16px">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
          <el-tag :type="stream.phase === 'error' ? 'danger' : 'primary'">{{ phaseLabel }}</el-tag>
          <el-tag v-if="stream.currentSection?.title && stream.phase === 'writing'" type="warning" effect="plain">
            当前章节: {{ stream.currentSection.title }}
          </el-tag>
          <el-tag v-if="stream.elapsedSeconds > 0" type="info" effect="plain">
        已运行 {{ formatDuration(stream.elapsedSeconds) }}
      </el-tag>
      <el-tag
        v-if="running && stream.waitingSeconds >= 2"
        type="warning"
        effect="plain"
      >
        当前模型处理中 {{ stream.waitingSeconds }} 秒
      </el-tag>
      <el-tag v-if="stream.phase === 'screening' && stream.screeningProgress" type="success" effect="plain">
        已处理 {{ stream.screeningProgress.processed }}/{{ stream.screeningProgress.total }} 篇
      </el-tag>
        </div>
        <el-progress
          v-if="stream.phase === 'screening' && stream.screeningProgress"
          :percentage="Math.round((stream.screeningProgress.processed / Math.max(stream.screeningProgress.total, 1)) * 100)"
          :indeterminate="stream.screeningProgress.status === 'started' && stream.screeningProgress.batch === 0"
          :duration="2"
          :format="() => stream.screeningProgress!.batch === 0
            ? `准备筛选，共 ${stream.screeningProgress!.total} 篇`
            : `${stream.screeningProgress!.processed}/${stream.screeningProgress!.total} 篇 · 第 ${stream.screeningProgress!.batch}/${stream.screeningProgress!.totalBatches} 批`"
          style="margin-top: 8px"
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
          style="margin-top: 8px"
        />
        <div
          v-if="stream.detail"
          style="margin-top: 10px; padding: 10px 12px; border-radius: 6px; background: #f5f7fa; color: #606266; line-height: 1.7"
        >
          {{ stream.detail }}
        </div>
        <div
          v-if="stream.phase === 'writing' && livePreview"
          style="margin-top: 10px; border: 1px solid #ebeef5; border-radius: 8px; overflow: hidden"
        >
          <div style="padding: 10px 12px; background: #fafafa; border-bottom: 1px solid #ebeef5; color: #606266; font-size: 13px">
            当前章节流式预览
          </div>
          <div style="padding: 12px; white-space: pre-wrap; line-height: 1.8; color: #303133; max-height: 320px; overflow: auto">
            {{ livePreview }}
          </div>
        </div>
      </div>
    </el-card>

    <!-- 两阶段·确认点:主题划分完成,必须由用户确认后才进入正文写作 -->
    <template v-if="awaitingConfirm">
      <el-alert
        :type="stream.plan?.classify_fallback ? 'error' : 'warning'"
        :closable="false"
        show-icon
        style="margin-top: 16px"
        :title="stream.plan?.classify_fallback
          ? '自动主题划分失败,当前仅 1 个兜底主题 — 强烈建议重新划分'
          : '主题划分完成,请确认后才会开始写作'"
        :description="stream.plan?.classify_fallback
          ? '模型未按要求输出分组结果(输出被截断或格式异常),已降级为单一主题兜底。请点「不满意,重新划分」重试;若多次失败请检查模型服务。确认前不会生成任何正文。'
          : '确认前不会生成任何正文。可修改主题名称、删除不需要的主题;确认后将按这些主题分节写作。'"
      />
      <el-card style="margin-top: 12px">
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span>② 主题划分确认</span>
            <span style="font-size: 12px; color: #909399">
              共 {{ editableGroups.length }} 个主题 ·
              覆盖 {{ editableGroups.reduce((n, g) => n + g.lit_ids.length, 0) }} 篇文献
            </span>
          </div>
        </template>
        <div
          v-for="(g, idx) in editableGroups"
          :key="idx"
          style="display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px dashed #ebeef5; flex-wrap: wrap"
        >
          <span style="color: #909399; font-size: 12px; min-width: 56px">主题 {{ idx + 1 }}</span>
          <el-input
            v-model="g.name"
            placeholder="主题名称"
            :disabled="running"
            style="width: 320px; max-width: 100%"
          />
          <el-tag type="primary" effect="plain">{{ g.lit_ids.length }} 篇</el-tag>
          <span
            :title="groupPreview(g.lit_ids)"
            style="flex: 1; min-width: 200px; color: #909399; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
          >
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
        </div>
        <el-empty
          v-if="editableGroups.length === 0"
          description="已无主题:全部删除将无法写作,请重新划分"
          :image-size="60"
        />
        <div style="margin-top: 14px; display: flex; gap: 12px">
          <el-button type="primary" size="large" :loading="running" @click="confirmPlan">
            确认主题,开始写作
          </el-button>
          <el-button size="large" :disabled="running" @click="rePlan">
            不满意,重新划分
          </el-button>
        </div>
      </el-card>
    </template>

    <el-collapse v-if="stream.screenedOutIds.length" style="margin-top: 16px">
      <el-collapse-item :title="`筛选剔除 · ${stream.screenedOutIds.length} 篇(点击展开明细)`" name="screened">
        <span style="color: #909399; font-size: 12px; line-height: 1.8; word-break: break-all">
          {{ stream.screenedOutIds.join(', ') }}
        </span>
      </el-collapse-item>
    </el-collapse>
    <el-alert
      v-if="stream.droppedCitations.length"
      type="error"
      :closable="false"
      style="margin-top: 8px"
      title="检测到幻觉引用已剥离"
    >
      {{ stream.droppedCitations.join(', ') }}
    </el-alert>

    <el-card v-if="stream.relevanceReport" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>文献相关性分级清单</span>
          <span style="font-size: 12px; color: #909399">
            共 {{ stream.relevanceReport.total }} 篇 ·
            高相关 {{ stream.relevanceReport.summary.high }} ·
            中相关 {{ stream.relevanceReport.summary.medium }} ·
            低相关 {{ stream.relevanceReport.summary.low }}
          </span>
        </div>
      </template>
      <div style="margin-bottom: 10px; color: #909399; font-size: 12px; line-height: 1.7">
        高相关标准（须同时满足）：核心关键词重合度 ≥
        {{ stream.relevanceReport.criteria.keyword_overlap_high }}%、研究领域完全贴合（≥
        {{ stream.relevanceReport.criteria.field_match_full }}）、研究方法可直接参考（≥
        {{ stream.relevanceReport.criteria.method_applicable }}）。低相关文献仅作背景补充，不得作为核心论据。
      </div>
      <el-table :data="stream.relevanceReport.items" size="small" max-height="420" border>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div style="padding: 8px 16px; color: #606266; line-height: 1.9; font-size: 13px">
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
        style="margin-top: 10px"
        title="已排除出引用配额的低相关文献"
      >
        {{ stream.relevanceReport.excluded_low_relevance.length }} 篇：{{
          stream.relevanceReport.excluded_low_relevance.map((r) => r.lit_id).join(', ')
        }}
      </el-alert>
    </el-card>

    <el-card v-if="stream.groups.length && !awaitingConfirm" style="margin-top: 16px">
      <template #header>文献分组</template>
      <div v-for="g in stream.groups" :key="g.name" style="margin-bottom: 6px">
        <el-tag>{{ g.name }}</el-tag>
        <span style="margin-left: 8px; color: #909399; font-size: 12px">{{ g.lit_ids.length }} 篇</span>
      </div>
    </el-card>

    <el-card v-if="stream.sections.length" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-size: 18px; font-weight: 600">{{ topicInput.trim() || '文献综述' }} 文献综述</span>
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

      <template v-for="(s, i) in stream.sections" :key="s.key">
        <h3 style="margin: 16px 0 8px">
          {{ s.title }}
          <el-tag v-if="s.citations.length" size="small" type="info" style="margin-left: 8px">
            {{ s.citations.length }} 引用
          </el-tag>
        </h3>
        <div style="white-space: pre-wrap; line-height: 1.8; color: #303133">{{ s.content }}</div>
        <el-divider v-if="i < stream.sections.length - 1" />
      </template>

      <el-divider />
      <h3 style="margin: 0 0 8px">参考文献</h3>
      <pre style="white-space: pre-wrap; font-family: inherit; line-height: 1.8; color: #303133">{{ stream.referenceList || '(等待生成...)' }}</pre>
    </el-card>

    <!-- v9.7 软门禁:核查未通过不再报废综述,报告在此醒目展示供人工复核 -->
    <el-card v-if="stream.qaResult && stream.phase === 'complete'" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>全流程质量核查报告</span>
          <span style="font-size: 12px; color: #909399">
            通过率 {{ Math.round((stream.qaResult.pass_rate ?? 0) * 100) }}%
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
      <div style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px">
        <el-tag
          v-for="c in stream.qaResult.checks"
          :key="c.check_id"
          :type="qaStatusTagType(c.status)"
          effect="plain"
        >
          {{ c.name }}:{{ c.status === 'pass' ? '通过' : c.status === 'fail' ? `${c.fail_count} 项不通过` : c.status === 'warn' ? `${c.warn_count} 项警告` : '跳过' }}
        </el-tag>
      </div>
      <el-collapse v-if="qaFailedIssues.length || qaWarnIssues.length" style="margin-top: 10px">
        <el-collapse-item
          v-if="qaFailedIssues.length"
          :title="`不通过的问题(${qaFailedIssues.length},点击展开)`"
        >
          <div
            v-for="(issue, i) in qaFailedIssues.slice(0, 30)"
            :key="i"
            style="font-size: 13px; color: #f56c6c; line-height: 1.8"
          >
            [{{ issue.code }}] {{ issue.message }}{{ issue.path ? `(${issue.path})` : '' }}
          </div>
          <div v-if="qaFailedIssues.length > 30" style="font-size: 12px; color: #909399">
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
            style="font-size: 13px; color: #e6a23c; line-height: 1.8"
          >
            [{{ issue.code }}] {{ issue.message }}{{ issue.path ? `(${issue.path})` : '' }}
          </div>
          <div v-if="qaWarnIssues.length > 30" style="font-size: 12px; color: #909399">
            其余 {{ qaWarnIssues.length - 30 }} 条从略。
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-empty v-else-if="!running" description="尚未生成综述" style="margin-top: 32px" />
  </div>
</template>
