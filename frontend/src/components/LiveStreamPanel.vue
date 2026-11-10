<!-- LLM 实时流面板:思考过程 + 正文输出的流式直播台。
     数据源 = writing store 的 currentReasoning / currentSection(J1 重放可重建);
     非流式阶段(筛选/核查)显示阶段走带 + 说明,不装作有逐字流。 -->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { storeToRefs } from 'pinia';
import { useWritingStore } from '@/stores/writing';

const store = useWritingStore();
const { stream, running } = storeToRefs(store);

const reasoning = computed(() => stream.value.currentReasoning ?? '');
const content = computed(() => stream.value.currentSection?.content ?? '');
const sectionTitle = computed(() => stream.value.currentSection?.title ?? '');
const reasoningLen = computed(() => reasoning.value.length);
const contentLen = computed(() => content.value.length);

const PHASE_LABELS: Record<string, string> = {
  planning: '准备',
  screening: '文献筛选',
  classify: '主题划分',
  await_confirm: '等待主题确认',
  writing: '正文写作',
  reference: '参考文献',
  qa: '质量核查',
  complete: '已完成',
  error: '出错',
  idle: '空闲',
};
const phaseLabel = computed(() => PHASE_LABELS[stream.value.phase] ?? stream.value.phase);

/** 有逐字流才显示流块;批处理阶段(筛选/核查)无逐字事件,如实说明。 */
const hasStreamText = computed(() => reasoningLen.value > 0 || contentLen.value > 0);
const batchHint = computed(() => {
  if (stream.value.phase === 'screening') {
    return '筛选是分批批处理(模型逐批返回结构化结果),此阶段没有逐字流;进入「通读/主题划分」与「正文写作」后,思考与正文将在这里逐字打印。';
  }
  if (stream.value.phase === 'qa') {
    return '质量核查逐项运行中,步骤结果见下方「实时工作记录」。';
  }
  if (stream.value.phase === 'reference') {
    return '正在按正文引用顺序编排参考文献列表(确定性渲染,无逐字流)。';
  }
  return '等待任务产生文本流…';
});

/** 走带行:阶段 detail + 最近几条工作记录(非流式阶段的主内容)。 */
const tickerLines = computed(() => {
  const lines: string[] = [];
  if (stream.value.detail) lines.push(stream.value.detail);
  const steps = stream.value.sectionSteps.slice(-4);
  for (const s of steps) {
    lines.push(`[${s.status === 'ok' ? '完成' : s.status === 'fail' ? '失败' : '运行中'}] ${s.label}${s.seconds != null ? `(${s.seconds}s)` : ''}`);
  }
  for (const item of stream.value.activityLog.slice(-4)) {
    lines.push(`${item.at} ${item.message}`);
  }
  return lines.slice(-8);
});

/** 自动跟随滚动:贴底时持续吸底;用户向上翻阅即暂停,回到底部恢复。 */
const bodyEl = ref<HTMLElement | null>(null);
const stick = ref(true);
const onScroll = () => {
  const el = bodyEl.value;
  if (!el) return;
  stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
};
watch([reasoningLen, contentLen, () => tickerLines.value.length], async () => {
  if (!stick.value) return;
  await nextTick();
  const el = bodyEl.value;
  if (el) el.scrollTop = el.scrollHeight;
});
onBeforeUnmount(() => { /* watcher 随组件销毁 */ });
</script>

<template>
  <div class="live-console" :class="{ 'is-running': running }">
    <div class="lc-head">
      <span class="lc-dot" :class="running ? 'lc-dot-run' : 'lc-dot-idle'" />
      <span class="lc-title">LLM 实时流</span>
      <span class="lc-meta">{{ phaseLabel }}</span>
      <span v-if="reasoningLen" class="lc-meta">思考 {{ reasoningLen }} 字</span>
      <span v-if="contentLen" class="lc-meta">输出 {{ contentLen }} 字</span>
      <span class="lc-flex" />
      <button type="button" class="lc-btn" :title="stick ? '点击暂停自动跟随' : '点击恢复自动跟随'" @click="stick = !stick">
        {{ stick ? '自动跟随' : '已暂停跟随' }}
      </button>
    </div>
    <div ref="bodyEl" class="lc-body" @scroll="onScroll">
      <template v-if="hasStreamText">
        <div v-if="reasoningLen" class="lc-block">
          <div class="lc-block-tag lc-tag-think">思考过程 · 流式</div>
          <div class="lc-text lc-reasoning">{{ reasoning }}</div>
        </div>
        <div v-if="contentLen" class="lc-block">
          <div class="lc-block-tag lc-tag-content">
            正文输出<template v-if="sectionTitle"> · {{ sectionTitle }}</template>
          </div>
          <div class="lc-text lc-content">{{ content }}</div>
        </div>
      </template>
      <div v-else class="lc-empty">
        <div v-for="(line, i) in tickerLines" :key="i" class="lc-line">{{ line }}</div>
        <div class="lc-hint">{{ batchHint }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.live-console {
  margin-top: 12px;
  border: 1px solid var(--rule-soft);
  background: #fffef9;
}
.lc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 14px;
  border-bottom: 1px solid var(--rule-soft);
  background: rgba(0, 0, 0, 0.02);
}
.lc-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
}
.lc-dot-run { background: var(--accent, #a03226); animation: lc-pulse 1.4s ease-in-out infinite; }
.lc-dot-idle { background: #9b8f78; }
@keyframes lc-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.lc-title {
  font-family: var(--font-display);
  font-size: 12.5px;
  letter-spacing: 0.08em;
  color: var(--ink-soft);
}
.lc-meta { font-size: 12px; color: #8a7d64; }
.lc-flex { flex: 1; }
.lc-btn {
  border: 1px solid var(--rule-soft);
  background: transparent;
  color: var(--ink-soft);
  font-size: 12px;
  padding: 2px 10px;
  cursor: pointer;
}
.lc-btn:hover { background: rgba(0, 0, 0, 0.04); }
.lc-body {
  max-height: 300px;
  min-height: 88px;
  overflow-y: auto;
  padding: 10px 14px;
}
.lc-block + .lc-block { margin-top: 10px; }
.lc-block-tag {
  font-size: 11.5px;
  letter-spacing: 0.06em;
  margin-bottom: 5px;
}
.lc-tag-think { color: #8a7d64; }
.lc-tag-content { color: var(--accent, #a03226); }
.lc-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.75;
  font-size: 12.5px;
}
.lc-reasoning {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  color: #6e6350;
  background: rgba(0, 0, 0, 0.025);
  border-left: 2px solid var(--rule-soft);
  padding: 8px 10px;
}
.lc-content { color: var(--ink); font-size: 13.5px; line-height: 1.9; }
.lc-empty { color: #6e6350; }
.lc-line {
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.lc-hint {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--rule-soft);
  font-size: 12px;
  color: #9b8f78;
}
</style>
