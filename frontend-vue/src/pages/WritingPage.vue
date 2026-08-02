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
  ElRadioButton,
  ElRadioGroup,
  ElTag,
} from 'element-plus';
import { useTopicStore } from '@/stores/topic';
import { usePapersStore } from '@/stores/papers';
import { saveReview } from '@/api/endpoints';
import { generateWritingStream, type StreamState } from '@/api/streaming';

const topicStore = useTopicStore();
const papersStore = usePapersStore();

const form = reactive({
  mode: 'locale' as 'locale' | 'theme',
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
  error: null,
});

onMounted(() => papersStore.refresh());

const selectedCount = computed(() => papersStore.selected.length);

const phaseLabel = computed(() => {
  switch (stream.value.phase) {
    case 'start': return '初始化...';
    case 'screening': return 'LLM 主题筛选中...';
    case 'classify': return '文献分类完成';
    case 'writing': {
      const p = stream.value.progress;
      return p ? `写作中 ${p.index + 1}/${p.total}` : '写作中...';
    }
    case 'reference': return '生成参考文献...';
    case 'complete': return '完成';
    case 'error': return '出错了';
    default: return '';
  }
});

const start = async () => {
  if (!topicStore.topic) {
    ElMessage.error('请先回"主题"页填写研究主题');
    return;
  }
  if (selectedCount.value === 0) {
    ElMessage.error('文献池为空,请先添加文献');
    return;
  }
  running.value = true;
  stream.value = {
    phase: 'idle', sections: [], groups: [], referenceList: '',
    screenedOutIds: [], droppedCitations: [], progress: null, error: null,
  };
  try {
    const finalResp = await generateWritingStream(
      {
        topic: topicStore.topic,
        papers: papersStore.selected,
        classify_mode: form.mode,
        do_screening: form.doScreening,
      },
      (s) => { stream.value = s; },
    );
    await saveReview(finalResp);
  } catch (e: any) {
    stream.value = { ...stream.value, phase: 'error', error: e.message ?? String(e) };
  } finally {
    running.value = false;
  }
};

const downloadMd = () => {
  const lines: string[] = [];
  lines.push(`# ${topicStore.topic} 文献综述`, '');
  for (const s of stream.value.sections) {
    lines.push(`## ${s.title}`, '', s.content, '');
  }
  lines.push('## 参考文献', '', stream.value.referenceList, '');
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `文献综述-${topicStore.topic}.md`;
  a.click();
  URL.revokeObjectURL(url);
};
</script>

<template>
  <div>
    <el-card>
      <template #header>综述写作(流式输出)</template>
      <div style="margin-bottom: 12px">
        <span style="margin-right: 8px; color: #606266">研究主题</span>
        <el-tag type="info" size="large">{{ topicStore.topic || '未设置' }}</el-tag>
        <span style="margin: 0 12px; color: #606266">已选</span>
        <el-tag type="success" size="large">{{ selectedCount }} 篇</el-tag>
      </div>
      <div style="margin-bottom: 12px">
        <span style="margin-right: 12px; color: #606266">分类方式</span>
        <el-radio-group v-model="form.mode" :disabled="running">
          <el-radio-button value="locale">国内外分类</el-radio-button>
          <el-radio-button value="theme">主题分类</el-radio-button>
        </el-radio-group>
        <el-checkbox v-model="form.doScreening" :disabled="running" style="margin-left: 16px">
          LLM 主题筛选
        </el-checkbox>
      </div>
      <el-button
        type="primary"
        size="large"
        :loading="running"
        :disabled="!topicStore.topic || selectedCount === 0"
        @click="start"
      >
        {{ running ? '生成中...' : '开始生成' }}
      </el-button>

      <div v-if="running || stream.phase !== 'idle'" style="margin-top: 16px">
        <el-tag :type="stream.phase === 'error' ? 'danger' : 'primary'">{{ phaseLabel }}</el-tag>
        <el-progress
          v-if="stream.progress"
          :percentage="Math.round(((stream.progress.index + 1) / stream.progress.total) * 100)"
          :format="() => `${stream.progress!.index + 1}/${stream.progress!.total} 章`"
          style="margin-top: 8px"
        />
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
          <span style="font-size: 18px; font-weight: 600">{{ topicStore.topic }} 文献综述</span>
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