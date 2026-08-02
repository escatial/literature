<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { ElContainer, ElHeader, ElMain, ElMenu, ElMenuItem } from 'element-plus';

const route = useRoute();
const active = computed(() => route.path);

const menus = [
  { path: '/', label: '主题' },
  { path: '/english', label: '英文检索' },
  { path: '/cn', label: '中文导入' },
  { path: '/pool', label: '文献池' },
  { path: '/writing', label: '写作' },
];
</script>

<template>
  <el-container class="app-shell">
    <el-header height="60px" class="app-header">
      <div class="brand">文献综述 Agent</div>
      <el-menu
        :default-active="active"
        mode="horizontal"
        router
        class="app-menu"
        :ellipsis="false"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          {{ m.label }}
        </el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: #f5f7fa;
}
.app-header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 24px;
  gap: 24px;
}
.brand {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
}
.app-menu {
  flex: 1;
  border-bottom: none;
}
.app-main {
  max-width: 1280px;
  margin: 0 auto;
  width: 100%;
  padding: 24px;
}
</style>