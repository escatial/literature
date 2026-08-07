/** 路由。*/
import { createRouter, createWebHashHistory } from 'vue-router';

const TopicPage = () => import('@/pages/TopicPage.vue');
const EnglishRetrievalPage = () => import('@/pages/EnglishRetrievalPage.vue');
const ChineseImportPage = () => import('@/pages/ChineseImportPage.vue');
const UnifiedRetrievalPage = () => import('@/pages/UnifiedRetrievalPage.vue');
const RetrievalTasksPage = () => import('@/pages/RetrievalTasksPage.vue');
const LiteraturePoolPage = () => import('@/pages/LiteraturePoolPage.vue');
const WritingPage = () => import('@/pages/WritingPage.vue');
const RemoteBrowserPage = () => import('@/pages/RemoteBrowserPage.vue');

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'topic', component: TopicPage, meta: { title: '研究主题' } },
    { path: '/retrieval', name: 'retrieval', component: UnifiedRetrievalPage, meta: { title: '统一检索' } },
    { path: '/english', name: 'english', component: EnglishRetrievalPage, meta: { title: '英文检索(旧)' } },
    { path: '/cn', name: 'cn', component: ChineseImportPage, meta: { title: '中文导入(旧)' } },
    { path: '/pool', name: 'pool', component: LiteraturePoolPage, meta: { title: '文献池' } },
    { path: '/writing', name: 'writing', component: WritingPage, meta: { title: '综述写作' } },
    { path: '/browser', name: 'browser', component: RemoteBrowserPage, meta: { title: '远程浏览器' } },
  ],
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? ''} · 文献综述 Agent`;
});

export default router;