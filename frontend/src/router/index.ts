/** 路由。*/
import { createRouter, createWebHashHistory } from 'vue-router';
import UnifiedRetrievalPage from '@/pages/UnifiedRetrievalPage.vue';
import LiteraturePoolPage from '@/pages/LiteraturePoolPage.vue';
import WritingPage from '@/pages/WritingPage.vue';
import CrawlerMonitorPage from '@/pages/CrawlerMonitorPage.vue';

// 本地工作台仅四个页面，直接预加载比懒加载更稳。长时间打开开发页面并经历
// HMR 后，旧的懒加载模块 URL 可能失效，表现为菜单已高亮但页面仍停在原路由。

// 需求2:全站移除粘贴引文手动导入,ChineseImportPage 不再注册;
// 旧的 /cn 路由重定向到统一检索,避免外部链接失效。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/writing' },
    { path: '/retrieval', name: 'retrieval', component: UnifiedRetrievalPage, meta: { title: '统一检索' } },
    { path: '/unified', redirect: '/retrieval' },
    { path: '/cn', redirect: '/retrieval' },
    { path: '/pool', name: 'pool', component: LiteraturePoolPage, meta: { title: '文献池' } },
    { path: '/writing', name: 'writing', component: WritingPage, meta: { title: '综述写作' } },
    { path: '/crawler', name: 'crawler', component: CrawlerMonitorPage, meta: { title: '爬虫监控' } },
  ],
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? ''} · 文献综述 Agent`;
});

export default router;
