<script setup lang="ts">
/**
 * 可复用的远程浏览器面板(画布渲染 + 鼠标键盘转发)。
 * 各业务页面只负责传 db_type 和 keyword,
 * 用户在画布中操作,事件回传到远程 Chromium 执行。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { ElAlert, ElButton, ElCard, ElInput, ElMessage, ElOption, ElSelect, ElTag } from 'element-plus';
import { http } from '@/api/http';

interface Props {
  /** 打开后立即跳转的目标 URL(供"知网/维普/万方"等同页面复用) */
  presetKeyword?: string;
  presetDbType?: 'cnki' | 'cqvip' | 'wanfang' | string;
  /** 自定义搜索 URL 模板,使用 {kw} 占位 */
  urlTemplate?: string;
  /** 是否允许用户改关键词 */
  editable?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  presetKeyword: '',
  presetDbType: 'cnki',
  urlTemplate: '',
  editable: true,
});

// 与后端 VIEWPORT 保持一致
const VIEW_W = 1280;
const VIEW_H = 800;

const form = ref({
  keyword: props.presetKeyword,
  dbType: props.presetDbType,
});

const sessionId = ref('');
const pageUrl = ref('');
const pageTitle = ref('');
const verification = ref(false);
const vtype = ref('none');
const connected = ref(false);
const loading = ref(false);
const canvasRef = ref<HTMLCanvasElement | null>(null);

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
  ws = new WebSocket(`${wsBase()}/api/automation/ws/${sid}`);
  ws.onopen = () => {
    connected.value = true;
  };
  ws.onclose = () => {
    connected.value = false;
  };
  ws.onerror = () => {
    connected.value = false;
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'frame') {
        drawFrame(msg.data);
        pageUrl.value = msg.url ?? '';
        pageTitle.value = msg.title ?? '';
        verification.value = !!msg.verification;
        vtype.value = msg.vtype ?? 'none';
      } else if (msg.type === 'error') {
        ElMessage.error(`远程浏览器错误: ${msg.data}`);
      }
    } catch {
      /* 忽略非 JSON */
    }
  };
};

const startSession = async () => {
  if (!form.value.keyword.trim()) {
    ElMessage.warning('请输入检索关键词');
    return;
  }
  loading.value = true;
  try {
    const { data } = await http.post('/automation/session', {
      keyword: form.value.keyword,
      db_type: form.value.dbType,
    });
    sessionId.value = data.session_id;
    verification.value = data.verification;
    vtype.value = data.vtype;
    connectWs(data.session_id);
    ElMessage.success('远程浏览器会话已创建');
  } catch (e: any) {
    ElMessage.error(`创建会话失败: ${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
};

const closeSession = async () => {
  if (ws) {
    ws.close();
    ws = null;
  }
  if (sessionId.value) {
    try {
      await http.delete(`/automation/session/${sessionId.value}`);
    } catch {
      /* 忽略 */
    }
  }
  sessionId.value = '';
  pageUrl.value = '';
  connected.value = false;
  verification.value = false;
};

// 画布坐标 -> 视口坐标
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
const onMouseDown = (ev: MouseEvent) => {
  dragging = true;
  send({ action: 'down', ...toViewport(ev) });
};
const onMouseUp = (ev: MouseEvent) => {
  dragging = false;
  send({ action: 'up', ...toViewport(ev) });
};
const onMouseMove = (ev: MouseEvent) => {
  if (dragging) send({ action: 'move', ...toViewport(ev) });
};
const onClick = (ev: MouseEvent) => send({ action: 'click', ...toViewport(ev) });
const onWheel = (ev: WheelEvent) => {
  ev.preventDefault();
  send({ action: 'scroll', dx: ev.deltaX, dy: ev.deltaY });
};
const onKeyDown = (ev: KeyboardEvent) => {
  if (ev.key.length === 1) send({ action: 'type', text: ev.key });
  else send({ action: 'key', key: ev.key });
};

defineExpose({
  /** 给父页面调用:打开指定 URL（关键词搜索） */
  open: async (keyword: string, dbType?: string) => {
    form.value.keyword = keyword;
    if (dbType) form.value.dbType = dbType;
    await startSession();
  },
  /** 给父页面调用:关闭会话 */
  close: () => closeSession(),
  /** 当前会话状态 */
  state: () => ({
    sessionId: sessionId.value,
    pageUrl: pageUrl.value,
    pageTitle: pageTitle.value,
    verification: verification.value,
    vtype: vtype.value,
  }),
});

onMounted(() => window.addEventListener('keydown', onKeyDown));
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown);
  closeSession();
});
</script>

<template>
  <div>
    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>远程浏览器</span>
          <el-tag :type="connected ? 'success' : 'info'">
            {{ connected ? '已连接' : '未连接' }}
          </el-tag>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="服务器端无头浏览器自动启动,搜索结果画面以 WebSocket 推流到此处。验证码/滑块/登录在下方画布直接操作即可。"
        style="margin-bottom: 12px"
      />

      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap">
        <el-select v-model="form.dbType" style="width: 140px" :disabled="!!sessionId || !props.editable">
          <el-option label="中国知网" value="cnki" />
          <el-option label="维普" value="cqvip" />
          <el-option label="万方" value="wanfang" />
        </el-select>
        <el-input
          v-model="form.keyword"
          placeholder="检索关键词"
          style="width: 320px"
          :disabled="!!sessionId || !props.editable"
          @keyup.enter="startSession"
        />
        <el-button type="primary" :loading="loading" :disabled="!!sessionId" @click="startSession">
          启动远程浏览器
        </el-button>
        <el-button type="danger" plain :disabled="!sessionId" @click="closeSession">
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
    </el-card>

    <el-card v-if="sessionId" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-size: 13px; color: #606266">{{ pageTitle || '(加载中...)' }}</span>
          <span style="font-size: 12px; color: #909399; word-break: break-all">{{ pageUrl }}</span>
        </div>
      </template>
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
        提示:点击画布后直接键盘输入;滑块验证请按住左键拖动;滚轮可滚动页面。
      </div>
    </el-card>
  </div>
</template>
