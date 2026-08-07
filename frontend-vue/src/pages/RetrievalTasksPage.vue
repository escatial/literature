<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElMessage, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { getRetrievalTask, listRetrievalTasks } from '@/api/endpoints';
import type { RetrievalTask } from '@/api/types';

const ACTIVE_TASK_KEY = 'lit-review-active-retrieval-task-id';
const router = useRouter();
const loading = ref(false);
const tasks = ref<RetrievalTask[]>([]);

const load = async () => {
  loading.value = true;
  try {
    tasks.value = await listRetrievalTasks();
  } catch (e: any) {
    ElMessage.error(`加载任务失败:${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
};

const refreshOne = async (taskId: string) => {
  const task = await getRetrievalTask(taskId);
  tasks.value = tasks.value.map((t) => (t.task_id === taskId ? task : t));
};

const openInEnglishPage = (row: unknown) => {
  const task = row as RetrievalTask;
  localStorage.setItem(ACTIVE_TASK_KEY, task.task_id);
  router.push('/english');
};

const statusType = (status: RetrievalTask['status']) => {
  if (status === 'succeeded') return 'success';
  if (status === 'failed') return 'danger';
  return 'warning';
};

onMounted(load);
</script>

<template>
  <el-card>
    <template #header>
      <div style="display: flex; justify-content: space-between; align-items: center">
        <span>英文检索任务</span>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </template>

    <el-table :data="tasks" stripe v-loading="loading">
      <el-table-column prop="topic" label="主题" min-width="180" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="progress" label="进度" width="80" />
      <el-table-column prop="query_used" label="检索词" min-width="220" show-overflow-tooltip />
      <el-table-column prop="total_after_filter" label="结果数" width="90" />
      <el-table-column prop="updated_at" label="更新时间" min-width="180" />
      <el-table-column label="操作" width="200">
        <template #default="{ row }">
          <el-button size="small" @click="refreshOne(row.task_id)">更新</el-button>
          <el-button size="small" type="primary" @click="openInEnglishPage(row)">查看结果</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>
