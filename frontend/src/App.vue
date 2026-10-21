<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import AppToast from '@/components/AppToast.vue';

const route = useRoute();
const active = computed(() => route.path);

// 需求2:全站移除粘贴引文,顶部导航不再有「/cn」入口
const navs = [
  { path: '/retrieval', label: '统一检索', no: '壹' },
  { path: '/pool', label: '文献池', no: '贰' },
  { path: '/writing', label: '写作', no: '叁' },
  { path: '/crawler', label: '爬虫监控', no: '附' },
];
</script>

<template>
  <div class="masthead-wrap">
    <header class="masthead">
      <router-link to="/retrieval" class="brand">
        <span class="brand-seal" aria-hidden="true">述</span>
        <span class="brand-text">
          <span class="brand-name">文献综述编辑部</span>
          <span class="brand-sub">Literature Review Press</span>
        </span>
      </router-link>
      <nav class="mast-nav">
        <router-link
          v-for="n in navs"
          :key="n.path"
          :to="n.path"
          class="mast-link"
          :class="{ active: active === n.path }"
        >
          <span class="mast-no">{{ n.no }}</span>{{ n.label }}
        </router-link>
      </nav>
      <div class="mast-edge" aria-hidden="true">手稿在编 ·稿件随到随审</div>
    </header>
    <div class="masthead-rules" aria-hidden="true"></div>
  </div>

  <main class="app-main">
    <router-view :key="route.fullPath" />
  </main>

  <!-- 全局吐司宿主:所有提醒从右上角弹出 -->
  <AppToast />
</template>

<style scoped>
.masthead-wrap {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--sheet);
}

.masthead {
  max-width: 1240px;
  margin: 0 auto;
  padding: 0 28px;
  height: 62px;
  display: flex;
  align-items: center;
  gap: 32px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  text-decoration: none;
  flex: none;
}

.brand-seal {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  background: var(--cinnabar);
  color: #fdf4ec;
  font-family: var(--font-display);
  font-size: 19px;
  font-weight: 700;
  border-radius: var(--radius-sm);
  box-shadow: inset 0 0 0 1.5px rgba(253, 244, 236, 0.35);
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.15;
}

.brand-name {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.08em;
}

.brand-sub {
  font-family: var(--font-display);
  font-size: 9.5px;
  color: var(--ink-faint);
  letter-spacing: 0.22em;
  text-transform: uppercase;
}

.mast-nav {
  display: flex;
  align-items: stretch;
  gap: 6px;
  flex: 1;
  height: 100%;
}

.mast-link {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 14px;
  font-size: 13.5px;
  color: var(--ink-soft);
  text-decoration: none;
  letter-spacing: 0.06em;
  transition: color 0.15s ease;
}

.mast-no {
  font-family: var(--font-display);
  font-size: 11px;
  color: var(--ink-ghost);
  transition: color 0.15s ease;
}

.mast-link:hover {
  color: var(--ink);
}

.mast-link.active {
  color: var(--cinnabar);
  font-weight: 600;
}

.mast-link.active .mast-no {
  color: var(--cinnabar);
}

.mast-link.active::after {
  content: '';
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 0;
  height: 2px;
  background: var(--cinnabar);
}

/* 报头右侧:楷体小注,像期刊版权页的一行小字 */
.mast-edge {
  font-family: var(--font-annotate);
  font-size: 12px;
  color: var(--ink-ghost);
  letter-spacing: 0.14em;
  white-space: nowrap;
  flex: none;
}

/* 期刊式双细线:粗一道 + 细一道 */
.masthead-rules {
  border-top: 2px solid var(--ink);
  border-bottom: 1px solid var(--rule-strong);
}

.app-main {
  max-width: 1240px;
  margin: 0 auto;
  width: 100%;
  padding: 26px 28px 64px;
  box-sizing: border-box;
}

@media (max-width: 900px) {
  .masthead {
    gap: 14px;
    padding: 0 16px;
  }
  .mast-edge {
    display: none;
  }
  .app-main {
    padding: 18px 16px 48px;
  }
}
</style>
