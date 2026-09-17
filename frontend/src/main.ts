/** 应用入口。*/
import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import { bootstrapApp } from '@/app/bootstrap';

/**
 * 每个新的页面文档都从空白任务开始。
 *
 * Pinia 本身只存在于当前 JS 页面实例，但旧版本曾把任务快照、主题和
 * task id 写入 localStorage/sessionStorage。只在组件卸载时 reset 不足以
 * 覆盖浏览器刷新或重新打开页面，因此在挂载应用前统一清理这些入口。
 */
function clearPreviousPageSession(): void {
  try {
    for (const key of [
      'lit_review.current_task_id',
      'lit_review_topic',
      'lit-review-unified-retrieval-v1',
      'writing.snapshot.v1',
    ]) {
      sessionStorage.removeItem(key);
      localStorage.removeItem(key);
    }
  } catch {
    // 隐私模式或禁用存储时，页面仍应正常启动。
  }
}

clearPreviousPageSession();

/** 应用启动:不再自动清空文献池(避免与写作页 fetchAll 抢时序造成 race condition)。
 *  用户若需清空,可在「文献池」页手动操作。
 *  历史数据仍在 retrieval_history 中,可在「统一检索-最近检索记录」查看/恢复。 */
function mountApp() {
  const app = createApp(App);
  app.use(createPinia());
  app.use(router);
  app.use(ElementPlus, { locale: zhCn });
  app.mount('#app');
}

bootstrapApp({
  mountApp,
  // 启动时不再清空,避免与 fetchAll 抢时序
  clearPapers: () => Promise.resolve(),
});
