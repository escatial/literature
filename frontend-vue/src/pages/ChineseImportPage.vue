<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElInput, ElMessage, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { usePapersStore } from '@/stores/papers';
import { parseChineseCitations } from '@/api/endpoints';
import { makeLitId } from '@/api/openalex';
import type { ImportCitation, PaperCreatePayload } from '@/api/types';

const router = useRouter();
const papersStore = usePapersStore();

const rawText = ref('');
const loading = ref(false);
const parsed = ref<ImportCitation[]>([]);

const runParse = async () => {
  if (!rawText.value.trim()) {
    ElMessage.warning('请粘贴知网 GB/T 7714 引文');
    return;
  }
  loading.value = true;
  try {
    const resp = await parseChineseCitations(rawText.value);
    parsed.value = resp.citations;
    if (resp.parsed_fail > 0) {
      ElMessage.warning(`解析完成:成功 ${resp.parsed_ok},失败 ${resp.parsed_fail}`);
    } else {
      ElMessage.success(`成功解析 ${resp.parsed_ok} 条`);
    }
  } catch (e: any) {
    ElMessage.error(`解析失败: ${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
};

const addToPool = async () => {
  const okItems = parsed.value.filter((c) => c.parsed_ok);
  if (okItems.length === 0) {
    ElMessage.warning('没有可入库的条目');
    return;
  }
  const payload: PaperCreatePayload[] = await Promise.all(
    okItems.map(async (c) => ({
      lit_id: await makeLitId(c.title, null),
      source: 'user_imported' as const,
      title: c.title,
      authors: c.authors.split(/[,;、]/).map((s) => s.trim()).filter(Boolean),
      journal: c.journal,
      year: c.year,
      volume: c.volume,
      issue: c.issue,
      pages: c.pages,
      abstract: null,
      doi: null,
      source_url: '',
      cited_by_count: 0,
      raw_citation: c.raw_text,
      selected: true,
    })),
  );
  await papersStore.addBatch(payload);
  router.push('/pool');
};
</script>

<template>
  <div>
    <el-card>
      <template #header>中文文献批量导入</template>
      <p style="color: #909399; margin-top: 0">
        在知网"查新(引文格式)"选中多条,复制粘贴到下方。每行一条 GB/T 7714-2025 引文,摘要行会自动跳过。
      </p>
      <el-input
        v-model="rawText"
        type="textarea"
        :rows="12"
        placeholder="[1]吴亮. 高职院校水产市场营销课程思政建设探索[J]. 黑龙江水产, 2025, 44 (5): 668-673.&#10;摘要:...&#10;[2]陈丽叶,王慧婷. ..."
      />
      <div style="margin-top: 12px; display: flex; gap: 12px">
        <el-button type="primary" :loading="loading" @click="runParse">解析引文</el-button>
        <el-button
          type="success"
          :disabled="parsed.filter((c) => c.parsed_ok).length === 0"
          @click="addToPool"
        >
          全部导入到文献池({{ parsed.filter((c) => c.parsed_ok).length }})
        </el-button>
        <el-button @click="router.push('/pool')">查看文献池 →</el-button>
      </div>
    </el-card>

    <el-card v-if="parsed.length" style="margin-top: 16px">
      <template #header>
        共 {{ parsed.length }} 条 · 解析成功 {{ parsed.filter((c) => c.parsed_ok).length }} ·
        失败 {{ parsed.filter((c) => !c.parsed_ok).length }}
      </template>
      <el-table :data="parsed" stripe>
        <el-table-column prop="authors" label="作者" min-width="140" show-overflow-tooltip />
        <el-table-column prop="title" label="题名" min-width="280" show-overflow-tooltip />
        <el-table-column prop="journal" label="刊名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="year" label="年" width="80" />
        <el-table-column label="卷(期)" width="100">
          <template #default="{ row }">
            {{ row.volume ?? '—' }}{{ row.issue ? `(${row.issue})` : '' }}
          </template>
        </el-table-column>
        <el-table-column prop="pages" label="页" width="100" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.parsed_ok ? 'success' : 'danger'">
              {{ row.parsed_ok ? '✓' : '✗' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>