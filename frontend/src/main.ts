/** 应用入口。*/
import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import { bootstrapApp } from '@/app/bootstrap';

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
