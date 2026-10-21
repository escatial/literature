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
import { http, apiBaseURL } from '@/api/http';
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

// ---------- v9.10: 文献拼接大文档(后续划分/写作的 LLM 输入) ----------
const corpusLoading = ref(false);
const corpusDialogVisible = ref(false);
const corpusText = ref("");
const corpusMeta = ref("");

const openCorpus = async () => {
  corpusLoading.value = true;
  try {
    const resp = await http.get("/papers/corpus-document");
    corpusText.value = resp.data.document ?? "";
    corpusMeta.value = `文献拼接大文档 · ${resp.data.papers} 篇 · ${resp.data.chars} 字符`;
    corpusDialogVisible.value = true;
  } finally {
    corpusLoading.value = false;
  }
};

const downloadCorpusDocx = async () => {
  // 下载必须走 axios(自动携带 X-Task-Id);window.open 发不了自定义头,
  // 后端会因任务上下文缺失落到空池而 404(线上实测)。
  const resp = await http.get("/papers/corpus-document/docx", {
    responseType: "blob",
  });
  const url = URL.createObjectURL(resp.data);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "文献拼接大文档.docx";
  anchor.click();
  URL.revokeObjectURL(url);
};
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
  <div class="pool-page">
    <!-- 馆藏总览:任务标识 + 页间导航 -->
    <el-card shadow="never" class="sheet">
      <div class="pool-head">
        <div class="pool-head-main">
          <span class="section-no">贰</span>
          <div class="pool-title-wrap">
            <span class="section-title">馆藏文献池</span>
            <el-tooltip
              placement="top"
              :content="`当前任务 ID:${session.currentTaskId}\n每个 task 拥有独立的文献池,互不可见、互不干扰。`"
            >
              <span class="task-chip">task · {{ session.shortTaskId() }}</span>
            </el-tooltip>
          </div>
          <span class="pool-count">共 <b class="figure-num">{{ store.pageMeta.total }}</b> 条 · 第 {{ store.pageMeta.page }} / {{ store.pageMeta.total_pages }} 页</span>
        </div>
        <div class="pool-actions">
          <el-button size="small" :loading="startingNewTask" @click="startNewTask">
            开始新任务
          </el-button>
          <el-button size="small" text bg @click="router.push('/unified')">
            去统一检索
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="importing"
            :disabled="!stats || stats.total === 0"
            @click="goWriting"
          >
            去写作({{ stats?.total ?? 0 }} 篇)
          </el-button>
          <el-button
            size="small"
            plain
            :loading="corpusLoading"
            :disabled="!stats || stats.total === 0"
            @click="openCorpus"
          >
            拼接文献大文档
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- 采编筛选:中英文独立数量/年份筛选 + 实时统计 + 年份分布图 -->
    <el-card shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-no">采</span>
            <span class="section-title">写作导入筛选与统计</span>
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
            text
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
          <div class="filter-group is-cn">
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
              <span class="filter-join">至</span>
              <el-select v-model="cnYearEnd" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in cnYearOptions" :key="`cne-${y}`" :label="String(y)" :value="y" />
              </el-select>
            </span>
          </div>

          <!-- 英文组(不区分核心/非核心) -->
          <div class="filter-group is-en">
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
              <span class="filter-join">至</span>
              <el-select v-model="enYearEnd" size="small" style="width: 92px">
                <el-option label="不限" :value="0" />
                <el-option v-for="y in enYearOptions" :key="`ene-${y}`" :label="String(y)" :value="y" />
              </el-select>
            </span>
          </div>
        </div>

        <!-- 统计:筛选后总数 + 中英文分类(中文细分核心/非核心) -->
        <div v-if="stats" class="ledger">
          <div class="ledger-cell">
            <div class="ledger-num figure-num">{{ stats.total }}</div>
            <div class="ledger-label">筛选后总计(篇)</div>
          </div>
          <div class="ledger-cell is-zh">
            <div class="ledger-num figure-num">{{ stats.cn_total }}</div>
            <div class="ledger-label">中文 · 核心 {{ stats.cn_core }} / 非核心 {{ stats.cn_non_core }}</div>
          </div>
          <div class="ledger-cell is-en">
            <div class="ledger-num figure-num">{{ stats.en_total }}</div>
            <div class="ledger-label">英文(篇)</div>
          </div>
        </div>

        <!-- 年份分布:纯 CSS 堆叠柱状图(中文赭金 / 英文黛青) -->
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
          <div class="chart-legend">
            <span class="legend-dot zh"></span>中文
            <span class="legend-dot en"></span>英文
          </div>
        </div>
      </div>
    </el-card>

    <!-- 书架:全部/中文/英文 浏览与管理 -->
    <el-card shadow="never" class="sheet" style="margin-top: 18px">
      <template #header>
        <div class="section-head">
          <div class="head-main">
            <span class="section-no">架</span>
            <span class="section-title">书架</span>
          </div>
        </div>
      </template>
      <el-tabs v-model="activeTab" class="shelf-tabs">
        <el-tab-pane label="全部" name="all" />
        <el-tab-pane label="中文" name="cn" />
        <el-tab-pane label="英文" name="en" />

        <div class="shelf-toolbar">
          <div class="shelf-pageinfo">
            每页
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
            共 {{ store.pageMeta.total }} 条
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
          <el-table-column width="46">
            <template #default="{ row }">
              <el-checkbox
                :model-value="(row as Paper).selected"
                @change="store.toggle(row as Paper)"
              />
            </template>
          </el-table-column>
          <el-table-column label="来源" width="76">
            <template #default="{ row }">
              <span class="lang-chip" :class="isCnSource((row as Paper).source) ? 'zh' : 'en'">
                {{ isCnSource((row as Paper).source) ? '中文' : '英文' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="标题 / 引文" min-width="380">
            <template #default="{ row }">
              <div class="paper-title">{{ (row as Paper).title }}</div>
              <div class="paper-cite">{{ renderCitation(row as Paper) }}</div>
              <div class="paper-id">{{ (row as Paper).lit_id }}</div>
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
          class="shelf-pager"
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

    <el-dialog
      v-model="corpusDialogVisible"
      :title="corpusMeta"
      width="72%"
      top="4vh"
    >
      <div class="corpus-note">
        每条 = 「作者（年份）认为：观点(摘要)」。这份大文档就是主题划分阶段进入 LLM 的真实输入;Word 版每条观点带脚注链接到文献著录。
      </div>
      <el-input type="textarea" :rows="20" :model-value="corpusText" readonly />
      <template #footer>
        <el-button @click="corpusDialogVisible = false">关闭</el-button>
        <el-button type="primary" @click="downloadCorpusDocx">导出 Word(脚注链接文献)</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ── 馆藏总览 ── */
.pool-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.pool-head-main {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.pool-title-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}
.task-chip {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-faint);
  border: 1px solid var(--rule);
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  cursor: help;
  background: var(--sheet-dim);
}
.pool-count {
  font-size: 12.5px;
  color: var(--ink-faint);
}
.pool-count .figure-num {
  color: var(--ink);
  font-size: 15px;
}
.pool-actions {
  display: flex;
  gap: 2px;
  flex-wrap: wrap;
}

/* ── 采编筛选 ── */
.filter-tip {
  color: var(--ink-ghost);
  cursor: help;
  font-size: 12px;
}
.export-filter {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 2px 0 14px;
  border-bottom: 1px dashed var(--rule-soft);
}
.filter-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-left: 10px;
  border-left: 3px solid var(--en);
}
.filter-group.is-cn {
  border-left-color: var(--zh);
}
.filter-label {
  font-family: var(--font-display);
  font-size: 13px;
  color: var(--ink);
  font-weight: 600;
  letter-spacing: 0.1em;
  min-width: 62px;
}
.filter-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--ink-soft);
}
.filter-join {
  margin: 0 2px;
  color: var(--ink-ghost);
}

/* 账目式统计:三栏大数,左缘墨线 */
.ledger {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
  margin: 16px 0 4px;
}
@media (max-width: 860px) {
  .ledger {
    grid-template-columns: 1fr;
  }
}
.ledger-cell {
  padding: 10px 16px 12px;
  border-left: 3px solid var(--rule-strong);
  background: var(--sheet-dim);
}
.ledger-cell.is-zh {
  border-left-color: var(--zh);
}
.ledger-cell.is-en {
  border-left-color: var(--en);
}
.ledger-num {
  font-size: 32px;
  line-height: 1.2;
  color: var(--ink);
}
.ledger-cell.is-zh .ledger-num {
  color: var(--zh);
}
.ledger-cell.is-en .ledger-num {
  color: var(--en);
}
.ledger-label {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ink-faint);
  letter-spacing: 0.02em;
}

/* 年份分布 */
.chart-wrap {
  margin-top: 16px;
}
.chart-title {
  font-family: var(--font-display);
  font-size: 13px;
  color: var(--ink-soft);
  font-weight: 600;
  margin-bottom: 10px;
  letter-spacing: 0.08em;
}
.chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 150px;
  overflow-x: auto;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--rule-strong);
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
  overflow: hidden;
}
.seg {
  width: 100%;
}
.seg.cn {
  background: var(--zh);
}
.seg.en {
  background: var(--en);
}
.chart-col:hover .seg.cn {
  background: #b8871f;
}
.chart-col:hover .seg.en {
  background: #3d737e;
}
.chart-x {
  margin-top: 5px;
  font-family: var(--font-display);
  font-size: 11px;
  color: var(--ink-faint);
  white-space: nowrap;
  font-variant-numeric: oldstyle-nums;
}
.chart-legend {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ink-faint);
}
.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 1px;
  margin: 0 5px 0 0;
  vertical-align: middle;
}
.legend-dot.zh {
  background: var(--zh);
}
.legend-dot.en {
  background: var(--en);
  margin-left: 14px;
}

/* ── 书架 ── */
.shelf-tabs :deep(.el-tabs__item) {
  font-size: 14px;
  letter-spacing: 0.08em;
}
.shelf-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin: 6px 0 10px;
  flex-wrap: wrap;
}
.shelf-pageinfo {
  color: var(--ink-faint);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
}
.shelf-pager {
  margin-top: 16px;
  justify-content: flex-end;
}

/* 语种签:小方块色签 */
.lang-chip {
  display: inline-block;
  font-size: 11px;
  letter-spacing: 0.08em;
  padding: 1px 7px;
  border-radius: var(--radius-sm);
  border: 1px solid;
}
.lang-chip.zh {
  color: var(--zh);
  background: var(--zh-bg);
  border-color: var(--zh-line);
}
.lang-chip.en {
  color: var(--en);
  background: var(--en-bg);
  border-color: var(--en-line);
}

/* 条目:衬线标题 + 著录小字 + 编号 */
.paper-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--ink);
  line-height: 1.5;
}
.paper-cite {
  margin-top: 2px;
  color: var(--ink-faint);
  font-size: 12px;
  word-break: break-all;
}
.paper-id {
  margin-top: 1px;
  color: var(--ink-ghost);
  font-size: 11px;
  font-family: var(--font-mono);
}

/* 大文档弹窗 */
.corpus-note {
  font-family: var(--font-annotate);
  color: var(--ink-faint);
  font-size: 12.5px;
  margin-bottom: 10px;
  line-height: 1.7;
}
</style>
