<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { ElAlert, ElButton, ElCard, ElCheckbox, ElDivider, ElEmpty, ElMessage, ElRadioButton, ElRadioGroup, ElTag } from 'element-plus';
import { useTopicStore } from '@/stores/topic';
import { usePapersStore } from '@/stores/papers';
import { generateWriting, saveReview } from '@/api/endpoints';
import type { WritingResponse } from '@/api/types';

const topicStore = useTopicStore();
const papersStore = usePapersStore();

const mode = ref<'locale' | 'theme'>('locale');
const doScreening = ref(true);
const loading = ref(false);
const result = ref<WritingResponse | null>(null);

onMounted(() => papersStore.refresh());

const selectedCount = computed(() => papersStore.selected.length);

const runGenerate = async () => {
  if (!topicStore.topic) {
    ElMessage.warning('请先回"主题"页填写研究主题');
    return;
  }
  if (selectedCount.value === 0) {
    ElMessage.warning('文献池为空,请先添加文献');
    return;
  }
  loading.value = true;
  result.value = null;
  try {
    const resp = await generateWriting({
      topic: topicStore.topic,
      papers: papersStore.selected,
      classify_mode: mode.value,
      do_screening: doScreening.value,
    });
    result.value = resp;
    // 保存到历史
    await saveReview(resp);
    ElMessage.success('综述已生成并保存');
  } catch (e: any) {
    ElMessage.error(`生成失败: ${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
};

const downloadMd = () => {
  if (!result.value) return;
  const lines: string[] = [];
  lines.push(`# ${result.value.topic} 文献综述`, '');
  for (const s of result.value.sections) {
    lines.push(`## ${s.title}`, '', s.content, '');
  }
  lines.push('## 参考文献', '', result.value.reference_list, '');
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `文献综述-${result.value.topic}.md`;
  a.click();
  URL.revokeObjectURL(url);
};
</script>

<template>
  <div>
    <el-card>
      <template #header>综述写作</template>
      <el-form-item label="研究主题">
        <el-tag type="info" size="large">{{ topicStore.topic || '未设置' }}</el-tag>
      </el-form-item>
      <el-form-item label="已选文献">
        <el-tag type="success" size="large">{{ selectedCount }} 篇</el-tag>
      </el-form-item>
      <el-form-item label="分类方式">
        <el-radio-group v-model="mode" :disabled="loading">
          <el-radio-button value="locale">国内外分类</el-radio-button>
          <el-radio-button value="theme">主题分类</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="doScreening" :disabled="loading">
          写作前 LLM 筛选主题不符文献
        </el-checkbox>
      </el-form-item>
      <el-button
        type="primary"
        size="large"
        :loading="loading"
        :disabled="!topicStore.topic || selectedCount === 0"
        @click="runGenerate"
      >
        {{ loading ? '生成中(1~3 分钟)...' : '开始生成' }}
      </el-button>
    </el-card>

    <template v-if="result">
      <el-alert
        v-if="result.screened_out_ids.length"
        type="warning"
        :closable="false"
        style="margin-top: 16px"
      >
        筛选剔除了 {{ result.screened_out_ids.length }} 篇主题不符文献:{{ result.screened_out_ids.join(', ') }}
      </el-alert>
      <el-alert
        v-if="result.dropped_citations.length"
        type="error"
        :closable="false"
        style="margin-top: 8px"
      >
        检测到 {{ result.dropped_citations.length }} 处幻觉引用已剥离:{{ result.dropped_citations.join(', ') }}
      </el-alert>

      <el-card style="margin-top: 16px">
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span style="font-size: 18px; font-weight: 600">{{ result.topic }} 文献综述</span>
            <el-button type="primary" plain @click="downloadMd">下载 Markdown</el-button>
          </div>
        </template>

        <div v-for="(s, i) in result.sections" :key="s.key">
          <h3 style="margin: 16px 0 8px">{{ s.title }}</h3>
          <div style="white-space: pre-wrap; line-height: 1.8; color: #303133">{{ s.content }}</div>
          <el-divider v-if="i < result.sections.length - 1" />
        </div>

        <el-divider />
        <h3 style="margin: 0 0 8px">参考文献</h3>
        <pre style="white-space: pre-wrap; font-family: inherit; line-height: 1.8; color: #303133">{{ result.reference_list || '无' }}</pre>
      </el-card>
    </template>

    <el-empty v-else-if="!loading" description="尚未生成综述" style="margin-top: 32px" />
  </div>
</template>