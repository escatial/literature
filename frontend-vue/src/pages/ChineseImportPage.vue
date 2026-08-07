<script setup lang="ts">
/**
 * 中文文献检索:默认通过远程浏览器检索国内数据库(知网/维普/万方),
 * 用户在画布内操作(处理验证码/滑块/登录),检索结果可一键抽条入库。
 * 同时保留「粘贴引文文本」作为 fallback(浏览器打不开时仍能工作)。
 */
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElInput,
  ElMessage,
  ElTabPane,
  ElTabs,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus';
import { http } from '@/api/http';
import { parseChineseCitations } from '@/api/endpoints';
import { makeLitId } from '@/api/openalex';
import { usePapersStore } from '@/stores/papers';
import type { ImportCitation, PaperCreatePayload } from '@/api/types';
import RemoteBrowserPanel from '@/components/RemoteBrowserPanel.vue';

const router = useRouter();
const papersStore = usePapersStore();
const browserRef = ref<InstanceType<typeof RemoteBrowserPanel> | null>(null);

// 浏览器模式:候选条目 / 勾选
const candidates = ref<PaperCreatePayload[]>([]);
const candidateSelected = ref<Set<string>>(new Set());
const extracting = ref(false);
const importing = ref(false);
const sessionId = ref('');

// 浏览器抓取的页面条目快照(供前端分享给 openalex ts)
interface BrowserCandidate {
  lit_id: string;
  source: string;
  title: string;
  authors: string[];
  journal?: string;
  year?: number | null;
  source_url?: string;
  selected?: boolean;
}

const extractFromBrowser = async () => {
  const state = browserRef.value?.state?.();
  if (!state?.sessionId) {
    ElMessage.warning('请先启动远程浏览器');
    return;
  }
  extracting.value = true;
  try {
    const { data } = await http.post<{
      session_id: string;
      url: string;
      items: BrowserCandidate[];
      count: number;
    }>('/automation/extract', null, {
      params: { session_id: state.sessionId, db_type: 'cnki' },
    });
    if (data.count === 0) {
      ElMessage.warning('当前页未发现文献条目,可点击下方"搜索结果"翻页后再试');
    } else {
      ElMessage.success(`发现 ${data.count} 个候选条目`);
    }
    candidates.value = data.items as unknown as PaperCreatePayload[];
    candidateSelected.value = new Set(
      data.items.map((it) => it.lit_id).filter(Boolean) as string[],
    );
    sessionId.value = data.session_id;
  } catch (e: any) {
    ElMessage.error(`提取失败:${e.message ?? e}`);
  } finally {
    extracting.value = false;
  }
};

const importChosenToPool = async () => {
  if (!sessionId.value) {
    ElMessage.warning('请先抽取候选');
    return;
  }
  const chosen = candidates.value.filter((c) => candidateSelected.value.has(c.lit_id));
  if (chosen.length === 0) {
    ElMessage.warning('请勾选要入库的条目');
    return;
  }
  importing.value = true;
  try {
    const { data } = await http.post<{
      inserted: number;
      updated: number;
    }>('/automation/import', {
      session_id: sessionId.value,
      db_type: 'cnki',
      chosen,
    });
    ElMessage.success(`已入库: 新增 ${data.inserted}, 更新 ${data.updated}`);
    await papersStore.refresh();
    router.push('/pool');
  } catch (e: any) {
    ElMessage.error(`入库失败:${e.message ?? e}`);
  } finally {
    importing.value = false;
  }
};

// ───────────── 粘贴引文文本(fallback) ─────────────

const rawText = ref('');
const parseLoading = ref(false);
const parsed = ref<ImportCitation[]>([]);

const runParse = async () => {
  if (!rawText.value.trim()) {
    ElMessage.warning('请粘贴知网 GB/T 7714 引文');
    return;
  }
  parseLoading.value = true;
  try {
    const resp = await parseChineseCitations(rawText.value);
    parsed.value = resp.citations;
    if (resp.parsed_fail > 0) {
      ElMessage.warning(`解析完成:成功 ${resp.parsed_ok},失败 ${resp.parsed_fail}`);
    } else {
      ElMessage.success(`成功解析 ${resp.parsed_ok} 条`);
    }
  } catch (e: any) {
    ElMessage.error(`解析失败:${e.message ?? e}`);
  } finally {
    parseLoading.value = false;
  }
};

const addParsedToPool = async () => {
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
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="中文文献现在通过远程浏览器检索知网/维普/万方。进入本页即启动,搜索后一键抽取条目入库。粘贴文本仍可作为备选。"
      style="margin-bottom: 16px"
    />

    <el-tabs>
      <!-- 默认 Tab:浏览器检索 -->
      <el-tab-pane label="🔍 浏览器检索(推荐)" name="browser">
        <RemoteBrowserPanel
          ref="browserRef"
          preset-db-type="cnki"
          :preset-keyword="''"
          :editable="true"
        />

        <el-card style="margin-top: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span>抽取候选 → 入库</span>
              <div style="display: flex; gap: 8px">
                <el-button
                  :loading="extracting"
                  :disabled="!browserRef"
                  @click="extractFromBrowser"
                >
                  抽取当前页面候选
                </el-button>
                <el-button
                  type="success"
                  :loading="importing"
                  :disabled="candidates.length === 0"
                  @click="importChosenToPool"
                >
                  导入勾选到文献池 ({{ candidateSelected.size }}/{{ candidates.length }})
                </el-button>
              </div>
            </div>
          </template>

          <el-table v-if="candidates.length" :data="candidates" stripe>
            <el-table-column width="50">
              <template #default="{ row }">
                <el-checkbox
                  :model-value="candidateSelected.has(row.lit_id)"
                  @change="
                    (v: unknown) => {
                      if (v) candidateSelected.add(row.lit_id);
                      else candidateSelected.delete(row.lit_id);
                      candidateSelected = new Set(candidateSelected);
                    }
                  "
                />
              </template>
            </el-table-column>
            <el-table-column label="标题" min-width="300" show-overflow-tooltip prop="title" />
            <el-table-column label="作者" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span>{{ (row.authors || []).slice(0, 3).join(', ') }}</span>
                <span v-if="(row.authors || []).length > 3">等</span>
              </template>
            </el-table-column>
            <el-table-column prop="year" label="年" width="80" />
            <el-table-column label="链接" width="80">
              <template #default="{ row }">
                <a v-if="row.source_url" :href="row.source_url" target="_blank">查看</a>
                <span v-else style="color: #c0c4cc">—</span>
              </template>
            </el-table-column>
          </el-table>

          <el-empty v-else description="请在上方浏览器中输入关键词检索(可翻页/查看详情),然后点击「抽取当前页面候选」" />
        </el-card>
      </el-tab-pane>

      <!-- 备选:粘贴引文文本 -->
      <el-tab-pane label="📋 粘贴引文文本(备选)" name="paste">
        <el-card>
          <template #header>粘贴 GB/T 7714 引文</template>
          <p style="color: #909399; margin-top: 0">
            从知网"查新(引文格式)"复制粘贴。每行一条,摘要行会自动跳过。
          </p>
          <el-input
            v-model="rawText"
            type="textarea"
            :rows="12"
            placeholder="[1]吴亮. 高职院校水产市场营销课程思政建设探索[J]. 黑龙江水产, 2025, 44 (5): 668-673.摘要:...&#10;[2]陈丽叶,王慧婷. ..."
          />
          <div style="margin-top: 12px; display: flex; gap: 12px">
            <el-button type="primary" :loading="parseLoading" @click="runParse">解析引文</el-button>
            <el-button
              type="success"
              :disabled="parsed.filter((c) => c.parsed_ok).length === 0"
              @click="addParsedToPool"
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
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
