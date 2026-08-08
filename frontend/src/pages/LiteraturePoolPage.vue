<script setup lang="ts">
/** 文献池:中英文解耦,通过 Tab 独立管理。检索/导入完成后自动入库,本页面只展示与管理。*/
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElMessage,
  ElMessageBox,
  ElPopconfirm,
  ElTable,
  ElTableColumn,
  ElTabPane,
  ElTabs,
  ElTag,
} from 'element-plus';
import { usePapersStore } from '@/stores/papers';
import type { Paper } from '@/api/types';

const router = useRouter();
const store = usePapersStore();
const activeTab = ref<'all' | 'cn' | 'en'>('all');

onMounted(() => store.refresh());

const renderCitation = (p: Paper): string => {
  if (p.source === 'user_imported' && p.raw_citation) return p.raw_citation;
  const authors = p.authors.join(', ') || 'Anon';
  const vol = p.volume ? (p.issue ? `${p.volume}(${p.issue})` : p.volume) : '';
  const tail = [p.journal, p.year ? String(p.year) : '', vol].filter(Boolean).join(', ');
  const pages = p.pages ? `: ${p.pages}` : '';
  return `${authors}. ${p.title}[J]. ${tail}${pages}.`;
};

const visiblePapers = computed<Paper[]>(() => {
  if (activeTab.value === 'cn') return store.cnPapers;
  if (activeTab.value === 'en') return store.enPapers;
  return store.papers;
});

// 1. 检索完成后,现在已自动入库,这里只保留手动单条导入作为冗余通道
// 2. 单独删除某一条,符合用户日常管理需求
const removeOne = async (p: Paper) => {
  try {
    await ElMessageBox.confirm(`确定删除:${p.title}?`, '确认', { type: 'warning' });
    await store.remove(p.lit_id);
    ElMessage.success('已删除');
  } catch {
    /* cancelled */
  }
};

const clearVisible = async () => {
  const tab = activeTab.value;
  // 区分范围:全部 / 仅中文 / 仅英文
  const isAll = tab === 'all';
  const label = isAll ? '整个文献池' : tab === 'cn' ? '中文库' : '英文库';
  try {
    await ElMessageBox.confirm(
      `确定清空${label}?此操作不可恢复。`,
      '警告',
      { type: 'warning' },
    );
    if (isAll) await store.clearAll();
    else await store.clearBySource(tab === 'cn' ? 'user_imported' : 'openalex');
    ElMessage.success('清空完成');
  } catch {
    /* cancelled */
  }
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <span>文献池</span>
          <div style="color: #909399; font-size: 13px">
            中文 {{ store.cnPapers.length }} · 英文 {{ store.enPapers.length }} · 已选
            {{ store.selected.length }} / 共 {{ store.papers.length }}
          </div>
        </div>
      </template>

      <!-- 顶部快捷跳转:从文献池快速回到检索/导入/写作页 -->
      <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px">
        <el-button size="small" @click="router.push('/english')">
          📥 打开英文检索
        </el-button>
        <el-button size="small" @click="router.push('/cn')">
          📥 打开中文导入
        </el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="store.papers.length === 0"
          @click="router.push('/writing')"
        >
          ✍️ 去写作({{ store.selected.length }} 篇)
        </el-button>
      </div>

      <div style="font-size: 12px; color: #909399; line-height: 1.6">
        • 英文文献：检索任务成功后<b>自动</b>写入文献池<br />
        • 中文文献：在「中文导入」页粘贴知网文本后<b>自动</b>写入<br />
        • 切换中英文 Tab 可独立删除/清空,互不影响
      </div>
    </el-card>

    <el-card style="margin-top: 16px">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="全部" name="all" />
        <el-tab-pane :label="`中文 (${store.cnPapers.length})`" name="cn" />
        <el-tab-pane :label="`英文 (${store.enPapers.length})`" name="en" />

        <div style="display: flex; justify-content: flex-end; gap: 8px; margin: 8px 0">
          <!-- 当前 Tab 对应的快捷入口 -->
          <el-button
            v-if="activeTab === 'cn'"
            size="small"
            @click="router.push('/cn')"
          >
            去导入更多中文 →
          </el-button>
          <el-button
            v-if="activeTab === 'en'"
            size="small"
            @click="router.push('/english')"
          >
            去检索更多英文 →
          </el-button>
          <el-popconfirm
            :title="`确定清空${activeTab === 'cn' ? '中文' : activeTab === 'en' ? '英文' : '所有'}文献?`"
            @confirm="clearVisible"
          >
            <template #reference>
              <el-button type="danger" plain size="small">清空</el-button>
            </template>
          </el-popconfirm>
        </div>

        <el-table
          v-if="visiblePapers.length"
          :data="visiblePapers"
          v-loading="store.loading"
          stripe
        >
          <el-table-column width="50">
            <template #default="{ row }">
              <el-checkbox
                :model-value="(row as Paper).selected"
                @change="store.toggle(row as Paper)"
              />
            </template>
          </el-table-column>
          <el-table-column label="来源" width="80">
            <template #default="{ row }">
              <el-tag
                :type="(row as Paper).source === 'user_imported' ? 'warning' : 'primary'"
                size="small"
              >
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
              <el-button type="danger" link @click="removeOne(row as Paper)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-empty
          v-else
          :description="
            activeTab === 'cn'
              ? '暂无中文文献,请到「中文导入」页粘贴知网文献'
              : activeTab === 'en'
              ? '暂无英文文献,请到「英文检索」页发起检索(成功后将自动入池)'
              : '文献池为空'
          "
        />
      </el-tabs>
    </el-card>
  </div>
</template>
