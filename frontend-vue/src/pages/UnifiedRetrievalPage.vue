<script setup lang="ts">
/**
 * 统一检索页:中英检索式生成 + 远程浏览器跨库轮询 + 入库。
 *
 * 状态全部进 useUnifiedRetrievalStore + localStorage,
 * 切走 tab 再回来,概念组/检索式/会话都还在。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElInput,
  ElMessage,
  ElTabs,
  ElTabPane,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus';
import { http } from '@/api/http';
import { queryPlan } from '@/api/endpoints';
import { useTopicStore } from '@/stores/topic';
import { usePapersStore } from '@/stores/papers';
import { useUnifiedRetrievalStore } from '@/stores/unifiedRetrieval';

const router = useRouter();
const topicStore = useTopicStore();
const papersStore = usePapersStore();
const ustore = useUnifiedRetrievalStore();

// 视图态:不持久化的(实时变化、来自 WS)
const pageUrl = ref('');
const pageTitle = ref('');
const connected = ref(false);
const canvasRef = ref<HTMLCanvasElement | null>(null);

// 候选条目:不持久化(画布会渲染实时结果)
const candidates = ref<any[]>([]);
const candidateSelected = ref<Set<string>>(new Set());
const extracting = ref(false);
const importing = ref(false);
const autoRunning = ref(false);
const autoProgress = ref({ pages: 0, count: 0 });
const multiRunning = ref(false);
const multiSummary = ref<{ count: number; perDb: any[] }>({ count: 0, perDb: [] });
const planningSummary = ref('');

// 派生
const sessionId = computed(() => ustore.sessionId);
const dbTypes = computed(() => ustore.dbTypes);
const currentIndex = computed(() => ustore.currentIndex);
const currentDb = computed(() => ustore.dbTypes[ustore.currentIndex] || '');
const verification = ref(false);
const vtype = ref('none');

const targets = ref<string[]>([]);  // 仅用于显示

// VIEWPORT 与后端保持一致
const VIEW_W = 1280;
const VIEW_H = 800;

let ws: WebSocket | null = null;
let img: HTMLImageElement | null = null;

const wsBase = (): string => {
  const base = (import.meta as any).env?.VITE_API_BASE || 'http://127.0.0.1:8080';
  return base.replace(/^http/, 'ws');
};

const drawFrame = (b64: string) => {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  if (!img) img = new Image();
  img.onload = () => ctx.drawImage(img as HTMLImageElement, 0, 0, VIEW_W, VIEW_H);
  img.src = `data:image/jpeg;base64,${b64}`;
};

const connectWs = (sid: string) => {
  // 如果已有 ws,先关
  if (ws) {
    ws.close();
    ws = null;
  }
  ws = new WebSocket(`${wsBase()}/api/automation/ws/${sid}`);
  ws.onopen = () => (connected.value = true);
  ws.onclose = () => (connected.value = false);
  ws.onerror = () => (connected.value = false);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'frame') {
        drawFrame(msg.data);
        pageUrl.value = msg.url ?? '';
        pageTitle.value = msg.title ?? '';
        verification.value = !!msg.verification;
        vtype.value = msg.vtype ?? 'none';
        if (typeof msg.current_index === 'number') {
          ustore.currentIndex = msg.current_index;
          ustore.persist();
        }
      } else if (msg.type === 'error') {
        ElMessage.error(`远程浏览器错误: ${msg.data}`);
      }
    } catch { /* ignore */ }
  };
};

/** 重新挂载时尝试 reconnect 已存在的会话(在切回 tab 时由 onMounted 调用)。 */
const tryReconnectSession = async () => {
  if (!ustore.sessionId) return;
  try {
    const { data } = await http.get(`/automation/session/${ustore.sessionId}`);
    // 同步会话状态
    targets.value = data.targets || [];
    ustore.dbTypes = data.db_types || [];
    ustore.currentIndex = data.current_index || 0;
    ustore.persist();
    verification.value = data.verification;
    vtype.value = data.vtype;
    pageUrl.value = data.url || '';
    pageTitle.value = data.title || '';
    connectWs(ustore.sessionId);
    ElMessage.success(`已恢复远程浏览器会话 (${data.db_types?.length || 0} 库)`);
  } catch (e: any) {
    // 后端会话已死,清掉 store 里的引用
    if (e?.response?.status === 404) {
      ustore.clearSession();
    }
  }
};

/** 调 LLM 生成检索式 */
const generatePlan = async () => {
  const t = ustore.topic.trim() || topicStore.topic.trim();
  if (!t) {
    ElMessage.warning('请先在「主题」页填写研究主题,或在此输入');
    return;
  }
  ustore.setTopic(t);
  ustore.setPlanning(true);
  planningSummary.value = '';
  try {
    const resp = await queryPlan(t, new Date().getFullYear() - 3);
    ustore.applyPlan({
      concepts: resp.concepts || [],
      field_zh: resp.field_zh || 'SU',
      field_en: resp.field_en || 'default',
      query_zh: resp.query_zh || t,
      query_en: resp.query_en || t,
    });
    planningSummary.value = `已拆 ${resp.concepts.length} 个概念,生成中英布尔式`;
    ElMessage.success(planningSummary.value);
  } catch (e: any) {
    ElMessage.error(`检索式生成失败:${e.message ?? e}`);
  } finally {
    ustore.setPlanning(false);
  }
};

/** 改同义词后实时重建 query */
const rebuildQueries = () => {
  if (ustore.concepts.length === 0) return;
  const zh = ustore.concepts
    .map((c) => "(" + (c.synonyms_zh.join("+") || c.label) + ")")
    .join(" * ");
  const en = ustore.concepts
    .map((c) => {
      const quoted = c.synonyms_en.map((s) =>
        s.includes(" ") && !s.startsWith('"') ? `"${s}"` : s,
      );
      return "(" + (quoted.join(" OR ") || c.label_en || c.label) + ")";
    })
    .join(" AND ");
  ustore.setQueries(zh, en);
};

/** 发送检索式到浏览器 */
const sendQueryToBrowser = async (submit = true) => {
  if (!ustore.sessionId) {
    ElMessage.warning('请先启动远程浏览器');
    return;
  }
  const isCn = currentDb.value === 'cnki' || currentDb.value === 'cqvip' || currentDb.value === 'wanfang';
  const query = isCn ? ustore.queryZh : ustore.queryEn;
  if (!query) {
    ElMessage.warning('当前库对应的检索式为空');
    return;
  }
  const loading = ElMessage.info({ message: '正在发送检索式到浏览器…', duration: 0 });
  // 兜底:15 秒后强制关闭 loading,避免 UI 一直转
  const forceClose = setTimeout(() => {
    try { loading.close(); } catch {}
    ElMessage.warning('发送超时(15s),可在浏览器里手动刷新或检查控制台');
  }, 15000);
  try {
    const r = await http.post('/automation/fill_query', {
      session_id: ustore.sessionId,
      query,
      submit,
      use_advanced: isCn,
      restrict_to_journals: isCn,
    });
    clearTimeout(forceClose);
    loading.close();
    if (r.data.ok) {
      ElMessage.success(
        `${submit ? '已填入并搜索' : '已填入(未提交)'} -> ${r.data.db}` +
        (isCn ? '(已高级检索 + 仅期刊)' : ''),
      );
    } else {
      ElMessage.warning(`填检索式失败: ${r.data.reason}`);
    }
  } catch (e: any) {
    clearTimeout(forceClose);
    loading.close();
    ElMessage.error(`失败: ${e.message ?? e}`);
    console.error('fill_query 异常:', e);
  }
};

/** 启动浏览器会话 */
const startSession = async () => {
  if (!ustore.queryZh.trim()) {
    ElMessage.warning('请先生成或填写检索式');
    return;
  }
  if (ustore.selectedDbs.length === 0) {
    ElMessage.warning('请至少选择一个数据库');
    return;
  }
  const body = {
    keyword: ustore.queryZh,
    keyword_en: ustore.queryEn || ustore.queryZh,
    db_types: ustore.selectedDbs,
  };
  try {
    const { data } = await http.post('/automation/session', body);
    ustore.setSession(data.session_id, data.db_types || ustore.selectedDbs, 0);
    targets.value = data.targets || [];
    verification.value = data.verification;
    vtype.value = data.vtype;
    candidates.value = [];
    candidateSelected.value = new Set();
    connectWs(data.session_id);
    ElMessage.success(`已启动 ${data.db_types.length} 个库轮询`);
    setTimeout(() => sendQueryToBrowser(true), 1500);
  } catch (e: any) {
    ElMessage.error(`启动失败:${e.message ?? e}`);
  }
};

const closeSession = async () => {
  if (ws) { ws.close(); ws = null; }
  if (ustore.sessionId) {
    try { await http.delete(`/automation/session/${ustore.sessionId}`); } catch { /* ignore */ }
  }
  ustore.clearSession();
  candidates.value = [];
  connected.value = false;
  verification.value = false;
};

/** 切换库 */
const switchDb = async (index: number) => {
  if (!ustore.sessionId) return;
  if (index === ustore.currentIndex) return;
  try {
    const r = await http.post('/automation/switch', {
      session_id: ustore.sessionId,
      index,
    });
    if (r.data.ok && !r.data.unchanged) {
      ustore.currentIndex = index;
      ustore.persist();
      ElMessage.success(`已切换到 ${ustore.dbTypes[index]}`);
    }
  } catch (e: any) {
    ElMessage.error(`切换失败:${e.message ?? e}`);
  }
};

const extractFromBrowser = async () => {
  if (!ustore.sessionId) {
    ElMessage.warning('请先启动远程浏览器');
    return;
  }
  extracting.value = true;
  try {
    const { data } = await http.post<{ items: any[]; count: number }>(
      '/automation/extract', null,
      { params: { session_id: ustore.sessionId, db_type: currentDb.value } },
    );
    if (data.count === 0) {
      ElMessage.warning('当前页未发现文献条目,可翻页后再试');
    } else {
      ElMessage.success(`发现 ${data.count} 个候选`);
    }
    candidates.value = data.items;
    candidateSelected.value = new Set(data.items.map((it: any) => it.lit_id).filter(Boolean));
  } catch (e: any) {
    ElMessage.error(`提取失败:${e.message ?? e}`);
  } finally {
    extracting.value = false;
  }
};

const autoExtract = async () => {
  if (!ustore.sessionId) {
    ElMessage.warning('请先启动远程浏览器');
    return;
  }
  autoRunning.value = true;
  autoProgress.value = { pages: 0, count: 0 };
  try {
    const { data } = await http.post<{ items: any[]; count: number; pages: number; stopped_reason: string }>(
      '/automation/auto_extract',
      {
        session_id: ustore.sessionId,
        target: ustore.autoTarget,
        max_pages: ustore.autoMaxPages,
        db_type: currentDb.value,
      },
    );
    if (data.count === 0) {
      ElMessage.warning('未抽取到任何条目');
    } else {
      const reason = {
        reached_target: '达成目标', max_pages: '达到最大翻页数',
        no_next: '已是末页', verification: '触发人机验证,已停止',
      }[data.stopped_reason] || data.stopped_reason;
      ElMessage.success(`已抽取 ${data.count} 条(${data.pages} 页,${reason})`);
    }
    candidates.value = data.items;
    candidateSelected.value = new Set(data.items.map((it: any) => it.lit_id).filter(Boolean));
    autoProgress.value = { pages: data.pages, count: data.count };
  } catch (e: any) {
    ElMessage.error(`自动抽取失败:${e.message ?? e}`);
  } finally {
    autoRunning.value = false;
  }
};

const multiExtract = async () => {
  if (!ustore.sessionId) return;
  multiRunning.value = true;
  try {
    const { data } = await http.post<{ items: any[]; count: number; per_db: any[]; exhausted: boolean }>(
      '/automation/multi_extract',
      {
        session_id: ustore.sessionId,
        target_per_db: ustore.autoTarget,
        max_pages_per_db: ustore.autoMaxPages,
        overall_target: Math.max(ustore.autoTarget * ustore.selectedDbs.length, 30),
      },
    );
    candidates.value = data.items;
    candidateSelected.value = new Set(data.items.map((it: any) => it.lit_id).filter(Boolean));
    multiSummary.value = { count: data.count, perDb: data.per_db };
    if (data.count === 0) {
      ElMessage.warning('所有库都未能抽到条目');
    } else if (data.exhausted) {
      ElMessage.success(`已抽 ${data.count} 条,所有库已穷尽`);
    } else {
      ElMessage.success(`已抽 ${data.count} 条`);
    }
  } catch (e: any) {
    ElMessage.error(`跨库抽取失败:${e.message ?? e}`);
  } finally {
    multiRunning.value = false;
  }
};

const importChosenToPool = async () => {
  if (!ustore.sessionId) return;
  const chosen = candidates.value.filter((c) => candidateSelected.value.has(c.lit_id));
  if (chosen.length === 0) {
    ElMessage.warning('请勾选要入库的条目');
    return;
  }
  importing.value = true;
  try {
    const { data } = await http.post<{ inserted: number; updated: number }>(
      '/automation/import',
      { session_id: ustore.sessionId, db_type: currentDb.value, chosen },
    );
    ElMessage.success(`入库:新增 ${data.inserted},更新 ${data.updated}`);
    await papersStore.refresh();
  } catch (e: any) {
    ElMessage.error(`入库失败:${e.message ?? e}`);
  } finally {
    importing.value = false;
  }
};

// ─────────────── 画布鼠标键盘事件转发 ───────────────
const toViewport = (ev: MouseEvent) => {
  const canvas = canvasRef.value;
  if (!canvas) return { x: 0, y: 0 };
  const rect = canvas.getBoundingClientRect();
  return {
    x: Math.round(((ev.clientX - rect.left) / rect.width) * VIEW_W),
    y: Math.round(((ev.clientY - rect.top) / rect.height) * VIEW_H),
  };
};
const send = (payload: Record<string, unknown>) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
};
let dragging = false;
const onMouseDown = (ev: MouseEvent) => { dragging = true; send({ action: 'down', ...toViewport(ev) }); };
const onMouseUp = (ev: MouseEvent) => { dragging = false; send({ action: 'up', ...toViewport(ev) }); };
const onMouseMove = (ev: MouseEvent) => { if (dragging) send({ action: 'move', ...toViewport(ev) }); };
const onClick = (ev: MouseEvent) => send({ action: 'click', ...toViewport(ev) });
const onWheel = (ev: WheelEvent) => { ev.preventDefault(); send({ action: 'scroll', dx: ev.deltaX, dy: ev.deltaY }); };
const onKeyDown = (ev: KeyboardEvent) => {
  if (ev.key.length === 1) send({ action: 'type', text: ev.key });
  else send({ action: 'key', key: ev.key });
};

// ─────────────── 同步顶层 topics 字段到 topicStore (并持久) ───────────────
watch(() => ustore.topic, (t) => {
  if (t && t !== topicStore.topic) {
    // 不强行覆盖 topicStore.topic(因为用户可能想独立编辑),但保持本组件可见
  }
});

onMounted(async () => {
  // 进页面先尝试 reconnect(如果有持久化的 session)
  if (ustore.sessionId) {
    tryReconnectSession();
  }
  window.addEventListener('keydown', onKeyDown);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown);
  // 不关 WS,跨页面共享会话;但关掉当前页面的 listeners
  if (ws) {
    // 保留 ws 引用在 store 之外不实际拿得到——下次进入会 reconnect
    // 为了避免重复 ws 句柄,这里把当前页面的 ws 也关掉
    ws.close();
    ws = null;
  }
});

const toggleDb = (db: string, enabled: boolean) => {
  const set = new Set(ustore.selectedDbs);
  if (enabled) set.add(db); else set.delete(db);
  ustore.setDbs([...set]);
};

const DB_LABELS: Record<string, string> = {
  cnki: '中国知网', wanfang: '万方', cqvip: '维普', pubmed: 'PubMed', openalex: 'OpenAlex',
};
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap">
          <span>📚 统一检索 - 中英文献一站式</span>
          <div style="display: flex; gap: 8px; align-items: center">
            <el-tag v-if="ustore.sessionId" type="success" size="small">
              会话已恢复 ({{ ustore.dbTypes.length }} 库)
            </el-tag>
            <el-tag type="info" size="small">主题:{{ topicStore.topic || '未设置' }}</el-tag>
          </div>
        </div>
      </template>

      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap">
        <el-input
          :model-value="ustore.topic"
          placeholder="研究主题(可编辑)"
          style="width: 320px"
          @input="(v: unknown) => ustore.setTopic(String(v))"
        />
        <el-button :loading="ustore.planning" @click="generatePlan">
          {{ ustore.planning ? '生成中…' : '生成中英检索式' }}
        </el-button>
      </div>

      <!-- 概念组可视化 -->
      <div
        v-if="ustore.concepts.length > 0"
        style="margin-top: 12px; padding: 10px 12px; background: #f0f9ff; border-radius: 6px"
      >
        <div
          v-if="ustore.concepts.length < 3"
          style="background: #fdf6ec; color: #e6a23c; padding: 8px 10px; border-radius: 4px;
                 margin-bottom: 8px; font-size: 12px"
        >
          ⚠ 仅识别出 {{ ustore.concepts.length }} 个概念,搜索结果可能不够精准。
          请手动编辑下方同义词,或点击"重新生成"再试。
        </div>
        <div style="font-size: 12px; color: #606266; margin-bottom: 6px; font-weight: 500">
          🎯 核心概念(改同义词后下方检索式自动重算)
        </div>
        <div
          v-for="c in ustore.concepts"
          :key="c.id"
          style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap"
        >
          <el-tag type="primary" size="small">{{ c.id }}</el-tag>
          <span style="font-weight: 500; min-width: 80px">{{ c.label }}</span>
          <span style="color: #909399; font-size: 12px">({{ c.label_en }})</span>
          <el-input
            :model-value="c.synonyms_zh.join(' / ')"
            placeholder="中文同义词,用 / 分隔"
            size="small"
            style="min-width: 280px"
            @change="(v: unknown) => { c.synonyms_zh = String(v).split('/').map(s => s.trim()).filter(Boolean); rebuildQueries(); }"
          />
          <el-input
            :model-value="c.synonyms_en.join(' / ')"
            placeholder="English synonyms, /-separated"
            size="small"
            style="min-width: 280px"
            @change="(v: unknown) => { c.synonyms_en = String(v).split('/').map(s => s.trim()).filter(Boolean); rebuildQueries(); }"
          />
        </div>
      </div>

      <!-- 字段 + 检索式 -->
      <div style="margin-top: 12px; display: grid; grid-template-columns: 100px 1fr; gap: 12px; align-items: center">
        <el-select
          :model-value="ustore.fieldZh"
          size="small"
          style="width: 110px"
          @change="(v: unknown) => { ustore.fieldZh = String(v); ustore.persist(); }"
        >
          <el-option label="主题(SU)" value="SU" />
          <el-option label="题名(TI)" value="TI" />
          <el-option label="关键词(KY)" value="KY" />
          <el-option label="摘要(AB)" value="AB" />
        </el-select>
        <el-input
          :model-value="ustore.queryZh"
          placeholder="中文检索式 - 知网高级检索语法"
          @input="(v: unknown) => ustore.setQueries(String(v), ustore.queryEn)"
        />
      </div>
      <div style="margin-top: 8px; display: grid; grid-template-columns: 100px 1fr; gap: 12px; align-items: center">
        <el-select
          :model-value="ustore.fieldEn"
          size="small"
          style="width: 110px"
          @change="(v: unknown) => { ustore.fieldEn = String(v); ustore.persist(); }"
        >
          <el-option label="默认" value="default" />
          <el-option label="title_abstract" value="title_abstract" />
        </el-select>
        <el-input
          :model-value="ustore.queryEn"
          placeholder="English boolean query"
          @input="(v: unknown) => ustore.setQueries(ustore.queryZh, String(v))"
        />
      </div>

      <div v-if="ustore.sessionId" style="margin-top: 8px; display: flex; gap: 8px; align-items: center">
        <el-button type="success" plain @click="sendQueryToBrowser(true)">
          📤 发送当前检索式到浏览器(填+回车)
        </el-button>
        <el-button @click="sendQueryToBrowser(false)">仅填入(不提交)</el-button>
        <span style="font-size: 12px; color: #909399">
          当前库 {{ DB_LABELS[currentDb] ?? currentDb }} 将收到
          <code style="background: #f5f5f5; padding: 0 4px; border-radius: 3px">
            {{ currentDb === 'openalex' || currentDb === 'pubmed' ? ustore.queryEn : ustore.queryZh }}
          </code>
        </span>
      </div>

      <div style="margin-top: 12px">
        <span style="margin-right: 8px; color: #606266">要轮询的库:</span>
        <el-checkbox
          :model-value="ustore.selectedDbs.includes('cnki')"
          @change="(v: unknown) => toggleDb('cnki', Boolean(v))"
        >{{ DB_LABELS.cnki }}</el-checkbox>
        <el-checkbox
          :model-value="ustore.selectedDbs.includes('wanfang')"
          @change="(v: unknown) => toggleDb('wanfang', Boolean(v))"
        >{{ DB_LABELS.wanfang }}</el-checkbox>
        <el-checkbox
          :model-value="ustore.selectedDbs.includes('cqvip')"
          @change="(v: unknown) => toggleDb('cqvip', Boolean(v))"
        >{{ DB_LABELS.cqvip }}</el-checkbox>
        <el-checkbox
          :model-value="ustore.selectedDbs.includes('pubmed')"
          @change="(v: unknown) => toggleDb('pubmed', Boolean(v))"
        >{{ DB_LABELS.pubmed }}</el-checkbox>
        <el-checkbox
          :model-value="ustore.selectedDbs.includes('openalex')"
          @change="(v: unknown) => toggleDb('openalex', Boolean(v))"
        >{{ DB_LABELS.openalex }}</el-checkbox>
      </div>

      <div style="margin-top: 12px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap">
        <el-button
          type="primary"
          :disabled="!ustore.sessionId && (!ustore.queryZh || ustore.selectedDbs.length === 0)"
          @click="startSession"
        >
          {{ ustore.sessionId ? '重启会话' : '启动远程浏览器' }}
        </el-button>
        <el-button type="danger" plain :disabled="!ustore.sessionId" @click="closeSession">
          关闭会话
        </el-button>
      </div>

      <el-alert
        v-if="verification"
        type="warning"
        :closable="false"
        show-icon
        style="margin-top: 12px"
        :title="`检测到人机验证(${vtype}),请在下方画布中完成验证`"
      />

      <div v-if="planningSummary" style="margin-top: 12px; color: #909399; font-size: 12px">
        {{ planningSummary }}
      </div>
    </el-card>

    <!-- 远程浏览器画布 -->
    <el-card v-if="ustore.sessionId" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px">
          <span style="font-size: 13px; color: #606266">{{ pageTitle || '(加载中...)' }}</span>
          <span style="font-size: 12px; color: #909399; word-break: break-all">{{ pageUrl }}</span>
        </div>
      </template>
      <el-tabs
        :model-value="String(ustore.currentIndex)"
        @tab-change="(idx: unknown) => switchDb(Number(idx))"
      >
        <el-tab-pane
          v-for="(db, i) in ustore.dbTypes"
          :key="db"
          :label="`${DB_LABELS[db] ?? db}${i === ustore.currentIndex ? ' (当前)' : ''}`"
          :name="String(i)"
        />
      </el-tabs>
      <div style="overflow: auto; border: 1px solid #e4e7ed; border-radius: 4px">
        <canvas
          ref="canvasRef"
          :width="VIEW_W"
          :height="VIEW_H"
          style="display: block; width: 100%; cursor: crosshair"
          tabindex="0"
          @mousedown="onMouseDown"
          @mouseup="onMouseUp"
          @mousemove="onMouseMove"
          @click="onClick"
          @wheel="onWheel"
        />
      </div>
      <div style="margin-top: 8px; color: #909399; font-size: 12px">
        提示:点击画布后键盘输入;滑块验证按住左键拖动;滚轮滚动。点击上方 Tab 一键切换到对应库。
      </div>
    </el-card>

    <!-- 抽取候选 + 入库 -->
    <el-card v-if="ustore.sessionId" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px">
          <span>📥 抽取候选 → 入库(当前库:{{ DB_LABELS[currentDb] ?? currentDb }})</span>
          <div style="display: flex; gap: 8px">
            <el-button :loading="extracting" @click="extractFromBrowser">仅当前页抽取</el-button>
            <el-button @click="router.push('/pool')">查看文献池 →</el-button>
          </div>
        </div>
      </template>

      <div
        style="background: #f5f7fa; border-radius: 6px; padding: 12px 16px; margin-bottom: 12px;
               display: flex; gap: 16px; align-items: center; flex-wrap: wrap"
      >
        <span style="color: #606266; font-weight: 500">📌 自动翻页抽取:</span>
        <span style="font-size: 12px; color: #909399">目标</span>
        <el-input-number
          :model-value="ustore.autoTarget"
          :min="5" :max="200" :step="5" size="small"
          @change="(v: unknown) => ustore.setAutoTarget(Number(v))"
        />
        <span style="font-size: 12px; color: #909399">条,翻页上限</span>
        <el-input-number
          :model-value="ustore.autoMaxPages"
          :min="1" :max="30" :step="1" size="small"
          @change="(v: unknown) => ustore.setAutoMaxPages(Number(v))"
        />
        <span style="font-size: 12px; color: #909399">页</span>
        <el-button
          type="primary"
          :loading="autoRunning"
          :disabled="verification"
          @click="autoExtract"
        >
          {{ autoRunning ? `抽取中... ${autoProgress.count} 条 / ${autoProgress.pages} 页` : '开始当前库抽取' }}
        </el-button>
        <el-button
          type="warning"
          plain
          :loading="multiRunning"
          :disabled="verification || ustore.selectedDbs.length === 0"
          @click="multiExtract"
        >
          {{ multiRunning ? '跨库抽取中...' : `跨 ${ustore.selectedDbs.length} 库自动抽取` }}
        </el-button>
      </div>

      <div
        v-if="multiSummary.perDb.length > 0"
        style="background: #f0f9ff; border-radius: 6px; padding: 10px 14px;
               margin-bottom: 12px; font-size: 12px; color: #303133"
      >
        <div style="margin-bottom: 4px; font-weight: 500">📊 跨库抽取汇总</div>
        <div
          v-for="(d, i) in multiSummary.perDb"
          :key="i"
          style="display: inline-block; margin-right: 12px"
        >
          <el-tag size="small">
            {{ d.db }} · {{ d.pages }} 页 · +{{ d.added }} 条 · {{ d.stopped_reason }}
          </el-tag>
        </div>
      </div>

      <div v-if="candidates.length" style="display: flex; gap: 8px; margin-bottom: 8px">
        <el-button type="success" :loading="importing" @click="importChosenToPool">
          导入勾选到文献池 ({{ candidateSelected.size }}/{{ candidates.length }})
        </el-button>
      </div>

      <el-table v-if="candidates.length" :data="candidates" stripe>
        <el-table-column width="50">
          <template #default="{ row }">
            <el-checkbox
              :model-value="candidateSelected.has(row.lit_id)"
              @change="
                (v: unknown) => {
                  if (v) candidateSelected.add(row.lit_id);
                  else candidateSelected.delete(row.lit_id);
                  candidateSelected = new Set(candidateSelected);
                }
              "
            />
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="300" show-overflow-tooltip prop="title" />
        <el-table-column label="作者" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ (row.authors || []).slice(0, 3).join(', ') }}</span>
            <span v-if="(row.authors || []).length > 3">等</span>
          </template>
        </el-table-column>
        <el-table-column prop="year" label="年" width="80" />
        <el-table-column label="链接" width="80">
          <template #default="{ row }">
            <a v-if="row.source_url" :href="row.source_url" target="_blank">查看</a>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-else description="请在上方浏览器中检索,然后点击「抽取当前页面候选」" />
    </el-card>

    <el-alert
      v-if="!ustore.sessionId"
      type="info"
      :closable="false"
      show-icon
      title="点击「启动远程浏览器」,会话创建后会立即自动填入检索式并搜索;切走其他 tab 再回来,会话会自动恢复。"
      style="margin-top: 16px"
    />
  </div>
</template>