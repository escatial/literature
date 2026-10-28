/** 路由。*/
import { createRouter, createWebHashHistory } from 'vue-router';
import UnifiedRetrievalPage from '@/pages/UnifiedRetrievalPage.vue';
import LiteraturePoolPage from '@/pages/LiteraturePoolPage.vue';
import WritingPage from '@/pages/WritingPage.vue';
import CrawlerMonitorPage from '@/pages/CrawlerMonitorPage.vue';
import LoginView from '@/pages/LoginView.vue';
import { getStoredToken } from '@/api/auth';

// 本地工作台仅五个页面，直接预加载比懒加载更稳。长时间打开开发页面并经历
// HMR 后，旧的懒加载模块 URL 可能失效，表现为菜单已高亮但页面仍停在原路由。

// 需求2:全站移除粘贴引文手动导入,ChineseImportPage 不再注册;
// 旧的 /cn 路由重定向到统一检索,避免外部链接失效。
// C7:/crawler 加 adminOnly —— 后端 B4 已收权,路由守卫只做体验层拦截
// (普通用户隐藏入口+直达回写作页),真正的权限判定在后端 403。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/writing' },
    { path: '/login', name: 'login', component: LoginView, meta: { title: '登录', public: true } },
    { path: '/retrieval', name: 'retrieval', component: UnifiedRetrievalPage, meta: { title: '统一检索' } },
    { path: '/unified', redirect: '/retrieval' },
    { path: '/cn', redirect: '/retrieval' },
    { path: '/pool', name: 'pool', component: LiteraturePoolPage, meta: { title: '文献池' } },
    { path: '/writing', name: 'writing', component: WritingPage, meta: { title: '综述写作' } },
    { path: '/crawler', name: 'crawler', component: CrawlerMonitorPage, meta: { title: '监控与管理', adminOnly: true } },
  ],
});

// 登录门禁:除 public 页面外,无令牌一律去 /login;已登录访问 /login 回写作页。
router.beforeEach(async (to) => {
  if (to.meta.public) {
    if (to.path === '/login' && getStoredToken()) return '/writing';
    return true;
  }
  if (!getStoredToken()) return '/login';
  if (to.meta.adminOnly) {
    // 角色不在令牌里,由 auth store 的已验证用户信息判定;
    // 用户信息尚未拉到(刷新直链)时先恢复会话,拉不到角色则放行交后端 403 兜底。
    const { useAuthStore } = await import('@/stores/auth');
    const auth = useAuthStore();
    if (auth.isLoggedIn && !auth.verified) await auth.restore();
    if (auth.user && !auth.isAdmin) return '/writing';
  }
  return true;
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? ''} · 文献综述 Agent`;
});

export default router;
