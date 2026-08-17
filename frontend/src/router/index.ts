/** 路由。*/
import { createRouter, createWebHashHistory } from 'vue-router';

const EnglishRetrievalPage = () => import('@/pages/EnglishRetrievalPage.vue');
const UnifiedRetrievalPage = () => import('@/pages/UnifiedRetrievalPage.vue');
const LiteraturePoolPage = () => import('@/pages/LiteraturePoolPage.vue');
const WritingPage = () => import('@/pages/WritingPage.vue');

// 需求2:全站移除粘贴引文手动导入,ChineseImportPage 不再注册;
// 旧的 /cn 路由重定向到统一检索,避免外部链接失效。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/retrieval' },
    { path: '/retrieval', name: 'retrieval', component: UnifiedRetrievalPage, meta: { title: '统一检索' } },
    { path: '/unified', redirect: '/retrieval' },
    { path: '/english', name: 'english', component: EnglishRetrievalPage, meta: { title: '英文检索(旧)' } },
    { path: '/cn', redirect: '/retrieval' },
    { path: '/pool', name: 'pool', component: LiteraturePoolPage, meta: { title: '文献池' } },
    { path: '/writing', name: 'writing', component: WritingPage, meta: { title: '综述写作' } },
  ],
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? ''} · 文献综述 Agent`;
});

export default router;