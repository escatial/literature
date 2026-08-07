<script setup lang="ts">
/** 英文文献检索页。
 *
 * 数据模型重大调整:
 *  - 检索任务成功后由后端自动批量入库到文献池 (papers.source=openalex)
 *  - 本页 results 直接绑定文献池中的英文文献(papersStore.enPapers)
 *  - 这样切换页面再回来,已入库的英文文献始终展示,不丢失
 *  - 顶部"导入文献池"按钮:把已勾选结果强制再同步一次(冗余保护),并跳转到文献池
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElForm,
  ElFormItem,
  ElInputNumber,
  ElMessage,
  ElMessageBox,
  ElPopconfirm,
  ElProgress,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus';
import { useTopicStore } from '@/stores/topic';
import { usePapersStore } from '@/stores/papers';
import { createRetrievalTask, deleteRetrievalTask, getRetrievalTask, listRetrievalTasks } from '@/api/endpoints';
import type { Paper, RetrievalTask } from '@/api/types';

const ACTIVE_TASK_KEY = 'lit-review-active-retrieval-task-id';
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
const currentTaskId = ref(localStorage.getItem(ACTIVE_TASK_KEY) || '');
const currentTask = ref<RetrievalTask | null>(null);
const taskHistory = ref<RetrievalTask[]>([]);
const queryUsed = ref('');
const selected = ref<Set<string>>(new Set());
let timer: number | null = null;

/** results 直接绑定文献池的英文部分,一旦 store 刷新(切回页面或入库)就自动呈现 */
const results = computed<Paper[]>(() => papersStore.enPapers);

/** 顶部"导入文献池"按钮:把当前勾选同步写入文献池,然后跳转 */
const importToPool = async () => {
  // 先确保 store 已是最新(防御性)
  await papersStore.refresh();
  const chosen = results.value.filter((p) => selected.value.has(p.lit_id));
  if (chosen.length === 0) {
    // 没勾选时,理解为"同步全部已入池的英文文献" -> 直接跳转
    ElMessage.success(`共 ${results.value.length} 篇已入池,正在前往文献池`);
    router.push('/pool');
    return;
  }
  await papersStore.addBatch(chosen.map((p) => ({ ...p, selected: true })));
  ElMessage.success(`已导入 ${chosen.length} 篇到文献池`);
  router.push('/pool');
};

const stopPolling = () => {
  if (timer != null) {
    window.clearInterval(timer);
    timer = null;
  }
};

const applyTask = (task: RetrievalTask) => {
  currentTask.value = task;
  queryUsed.value = task.query_used || '';
  loading.value = task.status === 'pending' || task.status === 'running';
  // 任务结束态:无论 succeeded/failed 都立刻清掉 loading 与 localStorage,
  // 避免页面跳转或刷新后残留为"后台检索中..."
  if (task.status === 'succeeded' || task.status === 'failed') {
    loading.value = false;
    localStorage.removeItem(ACTIVE_TASK_KEY);
    stopPolling();
  }
  if (task.status === 'succeeded') {
    ElMessage.success(`后台检索完成,${task.papers.length} 篇已自动入池`);
    papersStore.refresh();
  }
  if (task.status === 'failed') {
    ElMessage.error(`后台检索失败:${task.error || '未知错误'}`);
  }
};

const refreshHistory = async () => {
  taskHistory.value = await listRetrievalTasks();
};

const pollTask = async (taskId: string) => {
  const task = await getRetrievalTask(taskId);
  applyTask(task);
  await refreshHistory();
};

const startPolling = (taskId: string) => {
  stopPolling();
  pollTask(taskId).catch((e) => ElMessage.error(`恢复任务失败:${e.message ?? e}`));
  timer = window.setInterval(() => {
    pollTask(taskId).catch((e) => ElMessage.error(`查询任务失败:${e.message ?? e}`));
  }, 2000);
};

onMounted(async () => {
  // 仅在进入页面时拉取文献池的英文文献(已入池数据),不会触发任何远程检索
  await papersStore.refresh();
  selected.value = new Set(results.value.map((p) => p.lit_id));
  await refreshHistory();
  // 不再自动恢复旧的轮询任务;避免"进页面就看到在检索"的错觉
  // 用户想查看历史任务进度,在下方历史表中点"查看进度/继续轮询"
  currentTaskId.value = '';
});

onBeforeUnmount(() => stopPolling());

const runSearch = async () => {
  if (!topicStore.topic) {
    ElMessage.warning('请先回"主题"页填写研究主题');
    return;
  }
  selected.value.clear();
  queryUsed.value = '';
  loading.value = true;
  try {
    const created = await createRetrievalTask({
      topic: topicStore.topic,
      year_start: form.yearStart,
      year_end: form.yearEnd,
      min_citations: form.minCitations,
      limit: form.perSource,
      use_rerank: form.useRerank,
    });
    currentTaskId.value = created.task_id;
    localStorage.setItem(ACTIVE_TASK_KEY, created.task_id);
    ElMessage.success('已创建后台检索任务,可切换页面后回来查看');
    startPolling(created.task_id);
  } catch (e: any) {
    loading.value = false;
    ElMessage.error(`创建检索任务失败:${e.message ?? e}`);
  }
};

const restoreTask = (row: unknown) => {
  const task = row as RetrievalTask;
  currentTaskId.value = task.task_id;
  localStorage.setItem(ACTIVE_TASK_KEY, task.task_id);
  applyTask(task);
  if (task.status === 'pending' || task.status === 'running') {
    startPolling(task.task_id);
  }
};

/** 删除单个历史任务:连同当年自动入库到文献池的英文文献一起清理。
 *  与后端约定:不会影响用户手动入池的论文(仅 source='openalex' 会被清理)。
 */
const removeTask = async (row: unknown) => {
  const task = row as RetrievalTask;
  try {
    await ElMessageBox.confirm(
      `确定删除任务「${task.topic}」?\n将同步清理当年自动入库的英文文献,手动加入池的不会被删。`,
      '确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    const r = await deleteRetrievalTask(task.task_id);
    if (!r.task_deleted) {
      ElMessage.warning('任务不存在或已删除');
    } else {
      ElMessage.success(`任务已删除,清理文献 ${r.papers_deleted} 篇`);
      // 如果删的就是当前正在轮询/展示的任务,清理掉状态
      if (currentTaskId.value === task.task_id) {
        currentTaskId.value = '';
        localStorage.removeItem(ACTIVE_TASK_KEY);
        stopPolling();
        currentTask.value = null;
      }
      // 同步刷文献池 + 历史
      await papersStore.refresh();
      await refreshHistory();
    }
  } catch (e: any) {
    ElMessage.error(`删除失败:${e.message ?? e}`);
  }
};

const toggleOne = (litId: string) => {
  if (selected.value.has(litId)) selected.value.delete(litId);
  else selected.value.add(litId);
  selected.value = new Set(selected.value);
};

const toggleAll = (val: boolean) => {
  if (val) selected.value = new Set(results.value.map((p) => p.lit_id));
  else selected.value.clear();
};

const allChecked = computed(() =>
  results.value.length > 0 && results.value.every((p) => selected.value.has(p.lit_id)),
);
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <span>英文文献检索</span>
          <el-tag type="info">主题:{{ topicStore.topic || '未设置' }}</el-tag>
        </div>
      </template>

      <!-- 顶部操作栏:启动检索 / 导入文献池 -->
      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap">
        <el-button type="primary" :loading="loading" @click="runSearch">
          {{ loading ? '后台检索中...' : '开始后台检索' }}
        </el-button>
        <el-button
          type="success"
          :disabled="!results.length"
          @click="importToPool"
        >
          导入文献池 ({{ selected.size }}/{{ results.length }})
        </el-button>
        <el-button @click="router.push('/cn')">下一步:中文导入 →</el-button>
        <el-tag v-if="currentTask" :type="currentTask.status === 'failed' ? 'danger' : 'success'">
          {{ currentTask.status }} · {{ currentTask.progress }}%
        </el-tag>
      </div>

      <!-- 检索参数配置 -->
      <el-form inline style="margin-top: 12px">
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

      <el-progress
        v-if="currentTask && currentTask.status !== 'succeeded'"
        :percentage="currentTask.progress"
        style="margin-top: 12px"
      />
      <div v-if="queryUsed" style="margin-top: 12px; color: #909399; font-size: 13px">
        检索词:{{ queryUsed }}
      </div>

      <el-alert
        type="success"
        :closable="false"
        show-icon
        style="margin-top: 12px"
        title="本页直接展示文献池中已入库的英文文献。进入本页不会重新检索,只有点击「开始后台检索」才会发起新任务。"
      />
    </el-card>

    <el-card v-if="results.length" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>
            <el-checkbox
              :model-value="allChecked"
              @change="(v: unknown) => toggleAll(Boolean(v))"
            />
            共 {{ results.length }} 篇英文文献,已选 {{ selected.size }} 篇
          </span>
        </div>
      </template>
      <el-table :data="results" stripe>
        <el-table-column width="50">
          <template #default="{ row }">
            <el-checkbox
              :model-value="selected.has((row as Paper).lit_id)"
              @change="toggleOne((row as Paper).lit_id)"
            />
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="300">
          <template #default="{ row }">
            <div style="font-weight: 500">{{ (row as Paper).title }}</div>
            <div style="color: #909399; font-size: 12px">
              {{ (row as Paper).authors.slice(0, 3).join(', ') }}
              <span v-if="(row as Paper).authors.length > 3">等</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="journal" label="期刊" min-width="180" show-overflow-tooltip />
        <el-table-column prop="year" label="年份" width="80" />
        <el-table-column prop="cited_by_count" label="被引" width="80" />
      </el-table>
    </el-card>

    <el-alert
      v-else
      type="info"
      :closable="false"
      title="本页尚无英文文献。请先点击「开始后台检索」检索英文文献,完成后会自动入库。"
      style="margin-top: 16px"
    />

    <el-card v-if="taskHistory.length" style="margin-top: 16px">
      <template #header>历史检索任务</template>
      <el-table :data="taskHistory" stripe>
        <el-table-column prop="topic" label="主题" min-width="180" />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column prop="progress" label="进度" width="90" />
        <el-table-column prop="total_after_filter" label="结果" width="90" />
        <el-table-column prop="created_at" label="创建时间" min-width="180" />
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button size="small" @click="restoreTask(row)">查看进度/继续轮询</el-button>
            <el-popconfirm
              title="确定删除此任务?(会同步清理当年自动入库的英文文献)"
              confirm-button-text="删除"
              cancel-button-text="取消"
              @confirm="removeTask(row)"
            >
              <template #reference>
                <el-button size="small" type="danger" plain>删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
