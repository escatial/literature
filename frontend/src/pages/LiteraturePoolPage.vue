<script setup lang="ts">
/** 文献池:中英文 Tab + 服务端分页(需求5)。检索完成后自动入库,本页面只展示与管理。*/
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElMessage,
  ElMessageBox,
  ElPagination,
  ElPopconfirm,
  ElTable,
  ElTableColumn,
  ElTabPane,
  ElTabs,
  ElTag,
} from 'element-plus';
import {
  ALLOWED_PAGE_SIZES,
  DEFAULT_PAGE_SIZE,
  isCnSource,
  usePapersStore,
} from '@/stores/papers';
import type { Paper } from '@/api/types';

const router = useRouter();
const store = usePapersStore();

const activeTab = ref<'all' | 'cn' | 'en'>('all');
const pageSize = ref<number>(DEFAULT_PAGE_SIZE);
const currentPage = ref<number>(1);
/** 文献池总篇数(全部 tab 的 total,不受当前 tab 过滤影响),供「去写作」按钮展示 */
const poolTotal = ref(0);

watch(activeTab, async (v) => {
  currentPage.value = 1;
  await store.refresh({ page: 1, source: v });
});

watch(pageSize, async (size) => {
  currentPage.value = 1;
  store.setPageSize(size);
  await store.refresh({ page: 1, page_size: size });
});

watch(currentPage, async (page) => {
  await store.refresh({ page });
});

onMounted(async () => {
  // 每次进入文献池:重置回第 1 页 + 当前 tab,避免切走再回来时带着旧的 stale 分页状态
  currentPage.value = 1;
  await store.refresh({ page: 1 });
  poolTotal.value = store.pageMeta.total;
});

const renderCitation = (p: Paper): string => {
  if (!p) return '—';
  if (isCnSource(p.source) && p.raw_citation) return p.raw_citation;
  const authors = (p.authors || []).join(', ') || 'Anon';
  const vol = p.volume ? (p.issue ? `${p.volume}(${p.issue})` : p.volume) : '';
  const tail = [p.journal, p.year ? String(p.year) : '', vol].filter(Boolean).join(', ');
  const pages = p.pages ? `: ${p.pages}` : '';
  return `${authors}. ${p.title || ''}[J]. ${tail}${pages}.`;
};

const removeOne = async (p: Paper) => {
  try {
    await ElMessageBox.confirm(`确定删除:${p.title}?`, '确认', { type: 'warning' });
    await store.remove(p.lit_id);
    poolTotal.value = Math.max(0, poolTotal.value - 1);
    ElMessage.success('已删除');
  } catch {
    /* cancelled */
  }
};

const clearVisible = async () => {
  const tab = activeTab.value;
  const isAll = tab === 'all';
  const label = isAll ? '整个文献池' : tab === 'cn' ? '中文库' : '英文库';
  try {
    await ElMessageBox.confirm(
      `确定清空${label}?此操作不可恢复。`,
      '警告',
      { type: 'warning' },
    );
    if (isAll) {
      await store.clearAll();
      poolTotal.value = 0;
    }
    else if (tab === 'cn') {
      await store.clearBySource('user_imported');
      await store.clearBySource('cnki');
    } else {
      await store.clearBySource('openalex');
      await store.clearBySource('pubmed');
    }
    if (!isAll) {
      await store.refresh({ page: 1, source: 'all' });
      poolTotal.value = store.pageMeta.total;
      await store.refresh({ page: 1, source: tab });
    }
    ElMessage.success('清空完成');
  } catch {
    /* cancelled */
  }
};

const onJumpPage = (target: number | string) => {
  const n = Number(target);
  if (!Number.isFinite(n) || n < 1) {
    ElMessage.warning('请输入合法页码');
    return;
  }
  if (n > store.pageMeta.total_pages) {
    ElMessage.warning(`超过最大页码 ${store.pageMeta.total_pages}`);
    return;
  }
  currentPage.value = Math.floor(n);
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <span>文献池</span>
          <div style="color: #909399; font-size: 13px">
            共 {{ store.pageMeta.total }} 条 · 当前第 {{ store.pageMeta.page }} /
            {{ store.pageMeta.total_pages }} 页
          </div>
        </div>
      </template>

      <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px">
        <el-button size="small" @click="router.push('/unified')">
          🚀 打开统一检索
        </el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="poolTotal === 0"
          @click="router.push('/writing')"
        >
          ✍️ 去写作(共 {{ poolTotal }} 篇)
        </el-button>
      </div>


    </el-card>

    <el-card style="margin-top: 16px">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="全部" name="all" />
        <el-tab-pane label="中文" name="cn" />
        <el-tab-pane label="英文" name="en" />

        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: 8px 0; flex-wrap: wrap">
          <div style="color: #909399; font-size: 12px">
            每页显示
            <el-select
              v-model="pageSize"
              size="small"
              style="width: 96px; margin: 0 6px"
            >
              <el-option
                v-for="s in ALLOWED_PAGE_SIZES"
                :key="s"
                :label="`${s} 条`"
                :value="s"
              />
            </el-select>
            <span style="margin-left: 6px">共 {{ store.pageMeta.total }} 条</span>
          </div>
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
          v-if="store.papers.length"
          :data="store.papers"
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
                :type="isCnSource((row as Paper).source) ? 'warning' : 'primary'"
                size="small"
              >
                {{ isCnSource((row as Paper).source) ? '中文' : '英文' }}
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
              ? '暂无中文文献,请到「统一检索」页检索知网'
              : activeTab === 'en'
              ? '暂无英文文献,请到「统一检索」页检索 PubMed / OpenAlex'
              : '文献池为空'
          "
        />

        <el-pagination
          v-if="store.pageMeta.total > 0"
          style="margin-top: 16px; justify-content: flex-end"
          background
          layout="prev, pager, next, jumper, total"
          :total="store.pageMeta.total"
          :page-size="store.pageMeta.page_size"
          :current-page="store.pageMeta.page"
          @current-change="(p: number) => (currentPage = p)"
          @prev-click="(p: number) => (currentPage = p)"
          @next-click="(p: number) => (currentPage = p)"
        />
      </el-tabs>
    </el-card>
  </div>
</template>
