/** 应用入口。*/
import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import { clearPapers } from '@/api/endpoints';
import { bootstrapApp } from '@/app/bootstrap';

/** ★ 新会话语义:每次打开应用都是「新的任务」,文献池归零(不残留上次任务数据)。
 *  历史数据保存在 retrieval_history 中,可在「统一检索-最近检索记录」查看/恢复。 */
function mountApp() {
  const app = createApp(App);
  app.use(createPinia());
  app.use(router);
  app.use(ElementPlus, { locale: zhCn });
  app.mount('#app');
}

bootstrapApp({
  mountApp,
  clearPapers: () => clearPapers(),
});
