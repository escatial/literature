<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElDivider,
  ElEmpty,
  ElMessage,
  ElProgress,
  ElTag,
} from 'element-plus';
import { useTopicStore } from '@/stores/topic';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';
import { usePapersStore } from '@/stores/papers';
import { saveReview } from '@/api/endpoints';
import { generateWritingStream, type StreamState } from '@/api/streaming';
import type { Paper } from '@/api/types';

const topicStore = useTopicStore();
const ustore = useUnifiedRetrievalStore();
const papersStore = usePapersStore();

const form = reactive({
  mode: 'theme' as const,
  doScreening: true,
});

const running = ref(false);
const stream = ref<StreamState>({
  phase: 'idle',
  sections: [],
  groups: [],
  referenceList: '',
  screenedOutIds: [],
  droppedCitations: [],
  progress: null,
  currentSection: null,
  detail: null,
  error: null,
});

const allPapers = ref<Paper[]>([]);
const loadingPapers = ref(false);

onMounted(async () => {
  loadingPapers.value = true;
  try {
    allPapers.value = await papersStore.fetchAll();
  } catch (e) {
    ElMessage.error(`文献池加载失败:${String((e as Error)?.message ?? e)}`);
  } finally {
    loadingPapers.value = false;
  }
});

const selectedCount = computed(() => allPapers.value.length);

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
    case 'screening':
      return '主题筛选中';
    case 'classify':
      return '文献分组中';
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

const start = async () => {
  if (!topicInput.value.trim()) {
    ElMessage.error('请先填写研究主题(可在本页直接编辑)');
    return;
  }
  if (selectedCount.value === 0) {
    ElMessage.error('文献池为空,请先添加文献');
    return;
  }
  running.value = true;
  stream.value = {
    phase: 'idle',
    sections: [],
    groups: [],
    referenceList: '',
    screenedOutIds: [],
    droppedCitations: [],
    progress: null,
    currentSection: null,
    detail: null,
    error: null,
  };
  try {
    const finalResp = await generateWritingStream(
      {
        topic: topicInput.value.trim(),
        papers: allPapers.value,
        classify_mode: form.mode,
        do_screening: form.doScreening,
      },
      (s) => { stream.value = s; },
    );
    await saveReview(finalResp);
  } catch (e: any) {
    stream.value = {
      ...stream.value,
      phase: 'error',
      detail: e.message ?? String(e),
      error: e.message ?? String(e),
    };
  } finally {
    running.value = false;
  }
};

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
    <el-card>
      <template #header>综述写作</template>
      <div style="margin-bottom: 12px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px">
        <span style="color: #606266">研究主题</span>
        <el-input
          v-model="topicInput"
          placeholder="研究主题,如:无人机协同配送应急物资"
          size="large"
          clearable
          :disabled="running"
          style="width: 420px; max-width: 100%"
        />
        <span style="color: #606266">文献池</span>
        <el-tag type="success" size="large">{{ selectedCount }} 篇</el-tag>
      </div>
      <div style="margin-bottom: 12px">
        <span style="margin-right: 12px; color: #606266">按研究主题动态生成章节</span>
        <el-checkbox v-model="form.doScreening" :disabled="running" style="margin-left: 16px">
          LLM 主题筛选
        </el-checkbox>
      </div>
      <el-button
        type="primary"
        size="large"
        :loading="running"
        :disabled="!topicInput.trim() || selectedCount === 0 || loadingPapers"
        @click="start"
      >
        {{ running ? '生成中...' : '开始生成' }}
      </el-button>

      <div v-if="running || stream.phase !== 'idle'" style="margin-top: 16px">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
          <el-tag :type="stream.phase === 'error' ? 'danger' : 'primary'">{{ phaseLabel }}</el-tag>
          <el-tag v-if="stream.currentSection?.title && stream.phase === 'writing'" type="warning" effect="plain">
            当前章节: {{ stream.currentSection.title }}
          </el-tag>
          <el-tag v-if="stream.phase === 'writing' && liveChars > 0" type="success" effect="plain">
            已流式输出 {{ liveChars }} 字
          </el-tag>
        </div>
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

    <el-alert
      v-if="stream.screenedOutIds.length"
      type="warning"
      :closable="false"
      style="margin-top: 16px"
      title="筛选剔除"
    >
      {{ stream.screenedOutIds.length }} 篇:{{ stream.screenedOutIds.join(', ') }}
    </el-alert>
    <el-alert
      v-if="stream.droppedCitations.length"
      type="error"
      :closable="false"
      style="margin-top: 8px"
      title="检测到幻觉引用已剥离"
    >
      {{ stream.droppedCitations.join(', ') }}
    </el-alert>

    <el-card v-if="stream.groups.length" style="margin-top: 16px">
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

    <el-empty v-else-if="!running" description="尚未生成综述" style="margin-top: 32px" />
  </div>
</template>
