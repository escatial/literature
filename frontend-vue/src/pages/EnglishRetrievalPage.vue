<script setup lang="ts">
import { reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElCheckbox, ElForm, ElFormItem, ElInputNumber, ElMessage, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { useTopicStore } from '@/stores/topic';
import { usePapersStore } from '@/stores/papers';
import { queryPlan, rerankPapers } from '@/api/endpoints';
import { searchOpenAlex } from '@/api/openalex';
import type { Paper } from '@/api/types';

const router = useRouter();
const topicStore = useTopicStore();
const papersStore = usePapersStore();

const form = reactive({
  yearStart: 2020,
  yearEnd: new Date().getFullYear(),
  minCitations: 0,
  perSource: 20,
  useRerank: true,
});

const loading = ref(false);
const queryUsed = ref('');
const results = ref<Paper[]>([]);
const selected = ref<Set<string>>(new Set());

const applyFilters = (papers: Paper[]) =>
  papers.filter(
    (p) =>
      p.year >= form.yearStart &&
      p.year <= form.yearEnd &&
      p.cited_by_count >= form.minCitations,
  );

const dedup = (papers: Paper[]) => {
  const seen = new Set<string>();
  const out: Paper[] = [];
  for (const p of papers) {
    const doiKey = p.doi ? `doi:${p.doi.toLowerCase()}` : '';
    const titleKey = `t:${p.title.toLowerCase()}|${(p.authors[0] || '').toLowerCase()}|${p.year}`;
    if (doiKey && seen.has(doiKey)) continue;
    if (seen.has(titleKey)) continue;
    if (doiKey) seen.add(doiKey);
    seen.add(titleKey);
    out.push(p);
  }
  return out;
};

const runSearch = async () => {
  if (!topicStore.topic) {
    ElMessage.warning('请先回"主题"页填写研究主题');
    return;
  }
  loading.value = true;
  results.value = [];
  selected.value.clear();
  try {
    // 1. LLM 拆词
    const plan = await queryPlan(topicStore.topic, form.yearStart);
    const query = plan.query_str || topicStore.topic;
    queryUsed.value = query;

    // 2. 浏览器直连 OpenAlex
    const raw = await searchOpenAlex({
      query,
      yearStart: form.yearStart,
      yearEnd: form.yearEnd,
      perPage: form.perSource,
    });

    // 3. 过滤 + 去重
    let papers = applyFilters(raw);
    papers = dedup(papers);

    // 4. LLM 重排(可选)
    if (form.useRerank && papers.length > 0) {
      try {
        const ranked = await rerankPapers(plan.topic_summary, papers, form.perSource);
        papers = ranked.papers.map((p) => ({ ...p, selected: true }));
      } catch (e) {
        console.warn('rerank 失败,使用原顺序', e);
      }
    }

    results.value = papers;
    papers.forEach((p) => selected.value.add(p.lit_id));
    ElMessage.success(`找到 ${papers.length} 篇英文文献`);
  } catch (e: any) {
    ElMessage.error(`检索失败: ${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
};

const toggleOne = (litId: string) => {
  if (selected.value.has(litId)) selected.value.delete(litId);
  else selected.value.add(litId);
  selected.value = new Set(selected.value);
};

const addToPool = async () => {
  const chosen = results.value.filter((p) => selected.value.has(p.lit_id));
  if (chosen.length === 0) {
    ElMessage.warning('请先勾选至少 1 篇');
    return;
  }
  await papersStore.addBatch(chosen.map((p) => ({ ...p, selected: true })));
  router.push('/pool');
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>英文文献检索</span>
          <el-tag type="info">主题:{{ topicStore.topic || '未设置' }}</el-tag>
        </div>
      </template>
      <el-form inline>
        <el-form-item label="年份起">
          <el-input-number v-model="form.yearStart" :min="1900" :max="2100" />
        </el-form-item>
        <el-form-item label="年份止">
          <el-input-number v-model="form.yearEnd" :min="1900" :max="2100" />
        </el-form-item>
        <el-form-item label="最低被引">
          <el-input-number v-model="form.minCitations" :min="0" />
        </el-form-item>
        <el-form-item label="每源上限">
          <el-input-number v-model="form.perSource" :min="1" :max="200" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.useRerank">LLM 相关度重排</el-checkbox>
        </el-form-item>
      </el-form>
      <div style="display: flex; gap: 12px">
        <el-button type="primary" :loading="loading" @click="runSearch">
          {{ loading ? '检索中(OpenAlex 慢时 1~2 分钟)...' : '开始检索' }}
        </el-button>
        <el-button @click="router.push('/cn')">下一步:中文导入 →</el-button>
      </div>
      <div v-if="queryUsed" style="margin-top: 12px; color: #909399; font-size: 13px">
        检索词:{{ queryUsed }}
      </div>
    </el-card>

    <el-card v-if="results.length" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>共 {{ results.length }} 篇,已选 {{ selected.size }} 篇</span>
          <el-button type="success" :disabled="selected.size === 0" @click="addToPool">
            加入文献池({{ selected.size }})
          </el-button>
        </div>
      </template>
      <el-table :data="results" stripe>
        <el-table-column width="50">
          <template #default="{ row }">
            <el-checkbox
              :model-value="selected.has(row.lit_id)"
              @change="toggleOne(row.lit_id)"
            />
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="300">
          <template #default="{ row }">
            <div style="font-weight: 500">{{ row.title }}</div>
            <div style="color: #909399; font-size: 12px">
              {{ row.authors.slice(0, 3).join(', ') }}
              <span v-if="row.authors.length > 3">等</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="journal" label="期刊" min-width="180" show-overflow-tooltip />
        <el-table-column prop="year" label="年份" width="80" />
        <el-table-column prop="cited_by_count" label="被引" width="80" />
      </el-table>
    </el-card>
  </div>
</template>