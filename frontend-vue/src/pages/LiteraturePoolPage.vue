<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { ElButton, ElCard, ElCheckbox, ElMessageBox, ElPopconfirm, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { usePapersStore } from '@/stores/papers';
import type { Paper } from '@/api/types';

const store = usePapersStore();

onMounted(() => store.refresh());

const renderCitation = (p: Paper): string => {
  if (p.source === 'user_imported' && p.raw_citation) return p.raw_citation;
  const authors = p.authors.join(', ') || 'Anon';
  const vol = p.volume ? (p.issue ? `${p.volume}(${p.issue})` : p.volume) : '';
  const tail = [p.journal, p.year ? String(p.year) : '', vol].filter(Boolean).join(', ');
  const pages = p.pages ? `: ${p.pages}` : '';
  return `${authors}. ${p.title}[J]. ${tail}${pages}.`;
};

const cnCount = computed(() => store.cnPapers.length);
const enCount = computed(() => store.enPapers.length);
const selectedCount = computed(() => store.selected.length);

const clearAll = async () => {
  try {
    await ElMessageBox.confirm('确定清空文献池?此操作不可恢复。', '警告', {
      confirmButtonText: '清空',
      cancelButtonText: '取消',
      type: 'warning',
    });
    await store.clearAll();
  } catch {
    /* cancelled */
  }
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>文献池</span>
          <el-popconfirm title="确定清空?" @confirm="clearAll">
            <template #reference>
              <el-button type="danger" plain>清空</el-button>
            </template>
          </el-popconfirm>
        </div>
      </template>
      <div style="color: #909399">
        共 {{ store.papers.length }} 篇(中文 {{ cnCount }} / 英文 {{ enCount }}),已选
        {{ selectedCount }} 篇
      </div>
    </el-card>

    <el-card v-if="store.papers.length" style="margin-top: 16px">
      <el-table :data="store.papers" v-loading="store.loading" stripe>
        <el-table-column width="50">
          <template #default="{ row }">
            <el-checkbox :model-value="(row as Paper).selected" @change="store.toggle(row as Paper)" />
          </template>
        </el-table-column>
        <el-table-column label="来源" width="80">
          <template #default="{ row }">
            <el-tag :type="(row as Paper).source === 'user_imported' ? 'warning' : 'primary'" size="small">
              {{ (row as Paper).source === 'user_imported' ? '中文' : '英文' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="标题 / 引文" min-width="380">
          <template #default="{ row }">
            <div style="font-weight: 500">{{ (row as Paper).title }}</div>
            <div style="color: #909399; font-size: 12px; word-break: break-all">
              {{ renderCitation(row as Paper) }}
            </div>
            <div style="color: #c0c4cc; font-size: 11px; font-family: monospace">
              {{ (row as Paper).lit_id }}
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="journal" label="刊名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="year" label="年" width="80" />
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button type="danger" link @click="store.remove((row as Paper).lit_id)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-empty v-else description="文献池为空,请先到'英文检索'或'中文导入'页添加" />
  </div>
</template>