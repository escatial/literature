<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { useTopicStore } from '@/stores/topic';
import { ElCard, ElForm, ElFormItem, ElInput, ElButton, ElAlert } from 'element-plus';

const router = useRouter();
const topicStore = useTopicStore();
const topic = ref(topicStore.topic);

const save = () => {
  topicStore.setTopic(topic.value.trim());
  router.push('/english');
};
</script>

<template>
  <el-card>
    <template #header>研究主题</template>
    <el-alert
      v-if="!topic"
      type="info"
      :closable="false"
      title="输入你的研究方向或主题(如:水产品营销策略 / 人工智能医学影像 / 高等职业教育改革)"
      style="margin-bottom: 16px"
    />
    <el-form @submit.prevent="save">
      <el-form-item label="研究主题">
        <el-input
          v-model="topic"
          placeholder="例如:水产品营销策略"
          size="large"
          clearable
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" size="large" native-type="submit" :disabled="!topic.trim()">
          保存并进入英文检索 →
        </el-button>
      </el-form-item>
    </el-form>
  </el-card>
</template>