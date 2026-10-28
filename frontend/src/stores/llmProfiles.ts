/** 用户 LLM 供应商配置 store(多供应商列表 + 新增/编辑表单状态)。
 *
 * 后端:/api/auth/llm-profiles*(backend/src/api/auth.py)。
 * - 列表 api_key 只回显掩码,表单编辑留空 = 保持原 key;
 * - 「使用」= activate:后端把 profile 字段写入 users 表激活字段,
 *   运行时链路零改动;激活后同步 auth.user 保持「当前配置」一致。
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import {
  authApi,
  type LLMProfile,
  type LLMProfilePayload,
  type UpstreamModel,
} from '@/api/auth';
import { useAuthStore } from '@/stores/auth';
import { toast } from '@/utils/toast';

/** 新增/编辑表单状态(kind 联合收窄为 string 便于 radio 绑定)。*/
export interface LlmProfileForm {
  name: string;
  kind: 'openai' | 'anthropic' | string;
  base_url: string;
  api_key: string;
  model: string;
}

const EMPTY_FORM: LlmProfileForm = {
  name: '',
  kind: 'openai',
  base_url: '',
  api_key: '',
  model: '',
};

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: string } }; message?: string };
  return String(err?.response?.data?.detail ?? err?.message ?? fallback);
}

export const useLlmProfilesStore = defineStore('llmProfiles', () => {
  const auth = useAuthStore();

  // ── 列表状态 ──
  const profiles = ref<LLMProfile[]>([]);
  const loading = ref(false);

  // ── 表单状态:editingId = null 表示新增模式 ──
  const editingId = ref<number | null>(null);
  const form = ref<LlmProfileForm>({ ...EMPTY_FORM });
  const saving = ref(false);
  const probing = ref(false);
  const modelOptions = ref<UpstreamModel[]>([]);

  const editingProfile = computed<LLMProfile | null>(() => {
    if (editingId.value === null) return null;
    return profiles.value.find(p => p.profile_id === editingId.value) ?? null;
  });

  /** 编辑模式下 key 输入框的占位提示(回显原 key 掩码)。*/
  const apiKeyPlaceholder = computed<string>(() => {
    const masked = editingProfile.value?.api_key_masked;
    return masked ? `已保存 ${masked},留空表示不修改` : '请输入 API Key';
  });

  const kindHint = computed<string>(() =>
    form.value.kind === 'anthropic'
      ? 'Anthropic Messages 协议:适用于 Claude 官方 API 及其兼容网关(含 MiniMax 的 anthropic 端点)。'
      : 'OpenAI Chat Completions 协议:适用于 MiniMax、DeepSeek、通义、OpenRouter、Gemini OpenAI 端点等绝大多数平台。',
  );

  const baseUrlPlaceholder = computed<string>(() =>
    form.value.kind === 'anthropic'
      ? '例如 https://api.anthropic.com 或 https://api.minimax.cn/anthropic'
      : '例如 https://api.minimaxi.com/v1 或 https://openrouter.ai/api/v1',
  );

  /** 拉取(或刷新)当前用户的配置列表。*/
  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const { data } = await authApi.listLlmProfiles();
      profiles.value = data.profiles ?? [];
    } catch {
      /* http 拦截器已弹错 */
    } finally {
      loading.value = false;
    }
  }

  /** 重置为「新增」模式(清空表单)。*/
  function startCreate(): void {
    editingId.value = null;
    form.value = { ...EMPTY_FORM };
  }

  /** 进入「编辑」模式:回填除 key 外的字段(key 留空 = 不修改)。*/
  function startEdit(p: LLMProfile): void {
    editingId.value = p.profile_id;
    form.value = {
      name: p.name,
      kind: p.kind === 'anthropic' ? 'anthropic' : 'openai',
      base_url: p.base_url || '',
      api_key: '',
      model: p.model || '',
    };
  }

  /**
   * 保存表单到配置列表。
   * @param andActivate true = 「保存并使用」:保存成功后紧接着激活该条目
   * @returns 是否保存成功(失败时调用方无需处理,toast 已弹)
   */
  async function save(andActivate: boolean): Promise<boolean> {
    const name = form.value.name.trim();
    if (!name) {
      toast.warning('请先填写配置名');
      return false;
    }
    saving.value = true;
    try {
      const payload: LLMProfilePayload = {
        name,
        kind: form.value.kind,
        base_url: form.value.base_url.trim(),
        api_key: form.value.api_key.trim(),
        model: form.value.model.trim(),
      };
      let savedId: number;
      if (editingId.value === null) {
        const { data } = await authApi.createLlmProfile(payload);
        savedId = data.profile.profile_id;
        toast.success(`已保存「${name}」`);
      } else {
        savedId = editingId.value;
        await authApi.updateLlmProfile(savedId, payload);
        toast.success(`已更新「${name}」`);
      }
      await refresh();
      startCreate();
      if (andActivate) {
        const target = profiles.value.find(p => p.profile_id === savedId);
        if (target) await activate(target);
      }
      return true;
    } catch (e) {
      toast.error(errText(e, '保存失败'));
      return false;
    } finally {
      saving.value = false;
    }
  }

  /** 使用该配置(activate):后端写入 users 激活字段,同步 auth.user。*/
  async function activate(p: LLMProfile): Promise<void> {
    try {
      const { data } = await authApi.activateLlmProfile(p.profile_id);
      auth.user = data.user;
      await refresh();
      toast.success(data.message);
    } catch (e) {
      toast.error(errText(e, '切换失败'));
    }
  }

  /** 删除配置(调用方负责确认弹窗)。*/
  async function remove(p: LLMProfile): Promise<void> {
    try {
      await authApi.deleteLlmProfile(p.profile_id);
      if (editingId.value === p.profile_id) startCreate();
      await refresh();
      toast.success(`已删除「${p.name}」`);
    } catch (e) {
      toast.error(errText(e, '删除失败'));
    }
  }

  /** 从表单当前填写的上游拉取模型列表(实时探测,不落库)。*/
  async function probeModels(): Promise<void> {
    if (!form.value.base_url.trim()) {
      toast.warning('请先填写 Base URL');
      return;
    }
    probing.value = true;
    try {
      const { data } = await authApi.probeModels({
        llm_kind: form.value.kind,
        llm_base_url: form.value.base_url.trim(),
        llm_api_key: form.value.api_key.trim(),
      });
      modelOptions.value = data.models ?? [];
      toast.success(`获取到 ${modelOptions.value.length} 个模型`);
    } catch (e) {
      modelOptions.value = [];
      toast.error(errText(e, '获取模型列表失败'));
    } finally {
      probing.value = false;
    }
  }

  return {
    profiles, loading,
    editingId, form, saving, probing, modelOptions,
    editingProfile, apiKeyPlaceholder, kindHint, baseUrlPlaceholder,
    refresh, startCreate, startEdit, save, activate, remove, probeModels,
  };
});
