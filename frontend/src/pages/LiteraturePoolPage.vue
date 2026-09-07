<script setup lang="ts">
/** 文献池:中英文 Tab + 服务端分页(需求5)。
 *  「写作导入筛选与统计」卡片负责导入写作页的筛选/统计/去写作;
 *  列表区仅保留 全部/中文/英文 Tab 浏览与删除管理。*/
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElMessageBox,
  ElPagination,
  ElPopconfirm,
  ElSelect,
  ElOption,
  ElTable,
  ElTableColumn,
  ElTabPane,
  ElTabs,
  ElTag,
  ElTooltip,
} from 'element-plus';
import { toast } from '@/utils/toast';
import {
  ALLOWED_PAGE_SIZES,
  DEFAULT_PAGE_SIZE,
  isCnSource,
  usePapersStore,
} from '@/stores/papers';
import { exportPreviewPapers, batchSelectPapers } from '@/api/endpoints';
import type { ExportPreviewResponse, LangPoolRange, YearDistItem } from '@/api/endpoints';
import { useSessionStore } from '@/stores/session';
import type { Paper } from '@/api/types';

const router = useRouter();
const store = usePapersStore();
const session = useSessionStore();

const activeTab = ref<'all' | 'cn' | 'en'>('all');
const pageSize = ref<number>(DEFAULT_PAGE_SIZE);
const currentPage = ref<number>(1);

const refresh = async () => {
  currentPage.value = 1;
  await store.refresh({
    page: 1,
    source: activeTab.value,
  });
};

watch(activeTab, async (v) => {
  currentPage.value = 1;
  await store.refresh({
    page: 1,
    source: v,
  });
});

watch(pageSize, async (size) => {
  currentPage.value = 1;
  store.setPageSize(size);
  await refresh();
});

watch(currentPage, async (page) => {
  await store.refresh({ page });
});

onMounted(async () => {
  // 每次进入文献池:重置回第 1 页 + 当前 tab,避免切走再回来时带着旧的 stale 分页状态
  // (store.sourceFilter 是持久单例,只传 page 会沿用残留的 source;走 refresh() 保证
  // 请求条件与页面 UI 的 tab 严格一致)
  await refresh();
  // 初始加载「去写作」筛选预览统计(默认全选,fire-and-forget 不阻塞列表)
  void loadPreview();
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
    toast.success('已删除');
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
      await store.refresh({ page: 1, source: tab });
    }
    toast.success('清空完成');
  } catch {
    /* cancelled */
  }
};

const onJumpPage = (target: number | string) => {
  const n = Number(target);
  if (!Number.isFinite(n) || n < 1) {
    toast.warning('请输入合法页码');
    return;
  }
  if (n > store.pageMeta.total_pages) {
    toast.warning(`超过最大页码 ${store.pageMeta.total_pages}`);
    return;
  }
  currentPage.value = Math.floor(n);
};

/**
 * v7.0 任务隔离:开始一个全新的任务。
 * 行为:
 *   - 弹窗二次确认(防止误点丢数据)
 *   - 调 POST /papers/new-session,后端复用 currentTaskId 并清空该 task 的所有 papers
 *   - 刷新页面、跳回第 1 页
 * 隔离保证:历史 task 数据不被删除,只是「当前不可见」;切回老 task 时仍能看到。
 */
const startingNewTask = ref(false);
const startNewTask = async () => {
  if (startingNewTask.value) return;
  try {
    const total = store.pageMeta.total;
    const msg = total > 0
      ? `当前 task 已加载 ${total} 条文献。\n\n开始新任务将清空当前 task 的所有文献(其它历史 task 不受影响)。\n\n确定继续?`
      : '开始新任务?这会清空当前 task 的所有文献。';
    await ElMessageBox.confirm(msg, '开始新任务', { type: 'warning' });
  } catch {
    return; /* cancelled */
  }
  startingNewTask.value = true;
  try {
    const { reused, cleared } = await session.startNewTask();
    // 清空本地 store,然后重新拉
    await store.refresh({ page: 1, source: 'all' });
    toast.success(
      reused
        ? `已清空当前 task(${cleared} 条),task_id 保持不变`
        : `已生成新 task 并清空 ${cleared} 条`,
    );
  } catch (err) {
    toast.error(`开始新任务失败: ${(err as Error).message || err}`);
  } finally {
    startingNewTask.value = false;
  }
};

// ─────────────────────────────────────────────────────────────
// 写作导入筛选(去写作):中英文独立的数量/年份筛选 + 统计预览。
// 默认(全部条件不限)= 全选池内所有文献;「去写作(N 篇)」按筛选结果动态显示。
// ─────────────────────────────────────────────────────────────

/** 导入数量可选项;0 = 不限 */
const LIMIT_OPTIONS = [0, 50, 100, 150, 200, 300, 500];

/** 中文筛选状态 */
const cnLimit = ref(0);           // 中文导入数量,0=不限
const cnCoreOnly = ref(false);    // 中文仅核心期刊
const cnCoreLimit = ref(0);       // 中文核心导入数量(仅 cnCoreOnly=true 时生效)
const cnYearStart = ref(0);       // 中文起始年份,0=不限
const cnYearEnd = ref(0);         // 中文结束年份,0=不限
/** 英文筛选状态(英文不区分核心/非核心) */
const enLimit = ref(0);
const enYearStart = ref(0);
const enYearEnd = ref(0);

/** 筛选预览结果(统计 + lit_ids + 全池年份范围) */
const preview = ref<ExportPreviewResponse | null>(null);
const previewLoading = ref(false);

const buildPreviewReq = () => ({
    cn: { limit: cnLimit.value, year_start: cnYearStart.value, year_end: cnYearEnd.value },
    cn_core_only: cnCoreOnly.value,
    cn_core_limit: cnCoreLimit.value,
    en: { limit: enLimit.value, year_start: enYearStart.value, year_end: enYearEnd.value },
});

const loadPreview = async () => {
    previewLoading.value = true;
    try {
        preview.value = await exportPreviewPapers(buildPreviewReq());
    } catch (e) {
        console.error('[pool] export-preview failed:', e);
    } finally {
        previewLoading.value = false;
    }
};

// 筛选条件变化 → 防抖刷新预览统计
let previewTimer: ReturnType<typeof setTimeout> | undefined;
watch(
    [cnLimit, cnCoreOnly, cnCoreLimit, cnYearStart, cnYearEnd, enLimit, enYearStart, enYearEnd],
    () => {
        if (previewTimer) clearTimeout(previewTimer);
        previewTimer = setTimeout(loadPreview, 300);
    },
);

/** 年份下拉选项:自动识别池内文献的年份范围(不受当前筛选影响,选项保持稳定) */
const yearOptions = (r?: LangPoolRange | null): number[] => {
    if (!r || !r.min_year || !r.max_year || r.min_year > r.max_year) return [];
    const ys: number[] = [];
    for (let y = r.max_year; y >= r.min_year; y--) ys.push(y);
    return ys;
};
const cnYearOptions = computed(() => yearOptions(preview.value?.pool?.cn));
const enYearOptions = computed(() => yearOptions(preview.value?.pool?.en));

/** 筛选后统计(默认无筛选 = 全量文献统计) */
const stats = computed(() => preview.value?.filtered ?? null);

/** 是否处于手动筛选状态(决定「恢复全选」按钮显隐) */
const hasImportFilter = computed(
    () =>
        cnLimit.value > 0 || cnCoreOnly.value || cnCoreLimit.value > 0 ||
        cnYearStart.value > 0 || cnYearEnd.value > 0 ||
        enLimit.value > 0 || enYearStart.value > 0 || enYearEnd.value > 0,
);
const resetImportFilter = () => {
    cnLimit.value = 0;
    cnCoreOnly.value = false;
    cnCoreLimit.value = 0;
    cnYearStart.value = 0;
    cnYearEnd.value = 0;
    enLimit.value = 0;
    enYearStart.value = 0;
    enYearEnd.value = 0;
};

/** 年份分布图:柱高按最大值归一化(纯 CSS 柱状图,零图表依赖) */
const maxDistCount = computed(() =>
    Math.max(1, ...(stats.value?.year_distribution ?? []).map((d) => d.cn + d.en)),
);
const segHeight = (n: number) => `${Math.round((n / maxDistCount.value) * 100)}%`;

/** 去写作:把当前筛选结果批量导入写作池(replace),然后跳转写作页 */
const importing = ref(false);
const goWriting = async () => {
    if (importing.value) return;
    if (!stats.value || stats.value.total === 0) {
        toast.warning('筛选结果为空,请调整筛选条件或先检索文献');
        return;
    }
    importing.value = true;
    try {
        // 重新取一次最新预览,避免防抖期间筛选变化导致导入集合过期
        const latest = await exportPreviewPapers(buildPreviewReq());
        if (!latest.filtered.total) {
            toast.warning('筛选结果为空,请调整筛选条件');
            return;
        }
        const r = await batchSelectPapers(latest.filtered.lit_ids, 'replace');
        toast.success(`已导入 ${latest.filtered.total} 篇到写作页`);
        await router.push('/writing');
        void r;
    } catch (e) {
        toast.error(`导入失败:${String((e as Error)?.message ?? e)}`);
    } finally {
        importing.value = false;
    }
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
            <span>文献池</span>
            <!-- v7.0 任务隔离:显示当前 task 标识,避免多个 task 互相混淆 -->
            <el-tooltip
              placement="top"
              :content="`当前任务 ID:${session.currentTaskId}\n每个 task 拥有独立的文献池,互不可见、互不干扰。`"
            >
              <el-tag size="small" type="info" effect="plain">
                task: …{{ session.shortTaskId() }}
              </el-tag>
            </el-tooltip>
            <el-button
              size="small"
              :loading="startingNewTask"
              @click="startNewTask"
            >
              开始新任务
            </el-button>
          </div>
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
        <!-- 去写作:动态显示当前筛选后的文献总数,点击批量导入写作页 -->
        <el-button
          size="small"
          type="primary"
          :loading="importing"
          :disabled="!stats || stats.total === 0"
          @click="goWriting"
        >
          ✍️ 去写作({{ stats?.total ?? 0 }} 篇)
        </el-button>
      </div>
    </el-card>

    <!-- 写作导入筛选与统计:中英文独立数量/年份筛选 + 实时统计 + 年份分布图 -->
    <el-card style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
            <span>写作导入筛选与统计</span>
            <el-tooltip
              placement="top"
              content="设置筛选条件后,点击顶部「去写作」按钮,将筛选出的文献批量导入写作页。默认(全部不限)= 选中池内所有文献。"
            >
              <span class="filter-tip">ⓘ</span>
            </el-tooltip>
            <el-tag
              v-if="stats?.truncated"
              size="small"
              type="warning"
              effect="plain"
            >
              已达数量上限,按入库时间倒序截取
            </el-tag>
          </div>
          <el-button
            v-if="hasImportFilter"
            size="small"
            link
            type="primary"
            @click="resetImportFilter"
          >
            恢复全选
          </el-button>
        </div>
      </template>

      <div v-loading="previewLoading">
        <!-- 筛选区:中文 / 英文 两组独立条件 -->
        <div class="export-filter">
          <!-- 中文组 -->
          <div class="filter-group">
            <span class="filter-label">中文文献</span>
            <span class="filter-item">
              导入数量
              <el-select v-model="cnLimit" size="small" style="width: 100px">
                <el-option
                  v-for="n in LIMIT_OPTIONS"
                  :key="`cn-${n}`"
                  :label="n === 0 ? '不限' : `${n} 篇`"
                  :value="n"
                />
              </el-select>
            </span>
            <el-checkbox v-model="cnCoreOnly" size="small">仅核心期刊</el-checkbox>
            <template v-if="cnCoreOnly">
              <span class="filter-item">
                核心导入数量
                <el-select v-model="cnCoreLimit" size="small" style="width: 100px">
                  <el-option
                    v-for="n in LIMIT_OPTIONS"
                    :key="`cn-core-${n}`"
                    :label="n === 0 ? '不限' : `${n} 篇`"
                    :value="n"
                  />
                </el-select>
              </span>
            </template>
            <span class="filter-item">
              年份
              <el-select v-model="cnYearStart" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in cnYearOptions" :key="`cns-${y}`" :label="String(y)" :value="y" />
              </el-select>
              <span style="margin: 0 2px; color: #909399">至</span>
              <el-select v-model="cnYearEnd" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in cnYearOptions" :key="`cne-${y}`" :label="String(y)" :value="y" />
              </el-select>
            </span>
          </div>

          <!-- 英文组(不区分核心/非核心) -->
          <div class="filter-group">
            <span class="filter-label">英文文献</span>
            <span class="filter-item">
              导入数量
              <el-select v-model="enLimit" size="small" style="width: 100px">
                <el-option
                  v-for="n in LIMIT_OPTIONS"
                  :key="`en-${n}`"
                  :label="n === 0 ? '不限' : `${n} 篇`"
                  :value="n"
                />
              </el-select>
            </span>
            <span class="filter-item">
              年份
              <el-select v-model="enYearStart" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in enYearOptions" :key="`ens-${y}`" :label="String(y)" :value="y" />
              </el-select>
              <span style="margin: 0 2px; color: #909399">至</span>
              <el-select v-model="enYearEnd" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in enYearOptions" :key="`ene-${y}`" :label="String(y)" :value="y" />
              </el-select>
            </span>
          </div>
        </div>

        <!-- 统计模块:筛选后总数 + 中英文分类(中文细分核心/非核心) -->
        <div v-if="stats" class="stats-row">
          <div class="stat-card">
            <div class="stat-value">{{ stats.total }}</div>
            <div class="stat-label">筛选后总计(篇)</div>
          </div>
          <div class="stat-card">
            <div class="stat-value" style="color: #e6a23c">{{ stats.cn_total }}</div>
            <div class="stat-label">中文(核心 {{ stats.cn_core }} · 非核心 {{ stats.cn_non_core }})</div>
          </div>
          <div class="stat-card">
            <div class="stat-value" style="color: #409eff">{{ stats.en_total }}</div>
            <div class="stat-label">英文(篇)</div>
          </div>
        </div>

        <!-- 年份分布图:纯 CSS 堆叠柱状图(中文橙 / 英文蓝) -->
        <div v-if="stats && stats.year_distribution.length" class="chart-wrap">
          <div class="chart-title">年份分布(筛选后)</div>
          <div class="chart">
            <div
              v-for="(d, i) in stats.year_distribution"
              :key="d.year"
              class="chart-col"
              :title="`${d.year} 年:中文 ${d.cn} · 英文 ${d.en} · 共 ${d.cn + d.en} 篇`"
            >
              <div class="chart-bar">
                <div class="seg en" :style="{ height: segHeight(d.en) }"></div>
                <div class="seg cn" :style="{ height: segHeight(d.cn) }"></div>
              </div>
              <div class="chart-x">{{ i % 2 === 0 ? d.year : '' }}</div>
            </div>
          </div>
          <div style="margin-top: 6px; font-size: 12px; color: #909399">
            <span class="legend-dot" style="background: #e6a23c"></span>中文
            <span class="legend-dot" style="background: #409eff; margin-left: 12px"></span>英文
          </div>
        </div>
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

<style scoped>
.filter-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.filter-label {
  font-size: 13px;
  color: #606266;
  font-weight: 500;
}
.filter-tip {
  color: #909399;
  margin-left: 2px;
  cursor: help;
}

/* ── 写作导入筛选与统计 ── */
.export-filter {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 8px 0 12px;
  border-bottom: 1px dashed #ebeef5;
}
.filter-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #606266;
}
.stats-row {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin: 16px 0 8px;
}
.stat-card {
  flex: 1;
  min-width: 180px;
  padding: 12px 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  text-align: center;
  background: #fafafa;
}
.stat-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.3;
}
.stat-label {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
.chart-wrap {
  margin-top: 16px;
}
.chart-title {
  font-size: 13px;
  color: #606266;
  font-weight: 500;
  margin-bottom: 8px;
}
.chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 150px;
  overflow-x: auto;
  padding-bottom: 4px;
}
.chart-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 22px;
  flex: 1 0 auto;
}
.chart-bar {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  width: 14px;
  height: 120px;
  border-radius: 3px 3px 0 0;
  overflow: hidden;
  background: #f5f7fa;
}
.seg {
  width: 100%;
}
.seg.cn {
  background: #e6a23c;
}
.seg.en {
  background: #409eff;
}
.chart-x {
  margin-top: 4px;
  font-size: 11px;
  color: #909399;
  white-space: nowrap;
}
.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: middle;
}
</style>
