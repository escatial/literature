<script setup lang="ts">
/** 登录 / 注册页:注册制 + 管理员审核;系统预置默认管理员 admin/admin。*/
import { reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElAlert, ElButton, ElForm, ElFormItem, ElInput, ElTabPane, ElTabs } from 'element-plus';
import { useAuthStore } from '@/stores/auth';

const router = useRouter();
const auth = useAuthStore();

const mode = ref<'login' | 'register'>('login');
const loading = ref(false);
const errorMsg = ref('');
const infoMsg = ref('');

const form = reactive({
  username: '',
  password: '',
  password2: '',
  email: '',
});

const submitLogin = async () => {
  errorMsg.value = '';
  if (!form.username.trim() || !form.password) {
    errorMsg.value = '请输入用户名和密码';
    return;
  }
  loading.value = true;
  try {
    await auth.login(form.username.trim(), form.password);
    router.push('/writing');
  } catch (e: any) {
    errorMsg.value = String(e?.response?.data?.detail ?? e?.message ?? '登录失败');
  } finally {
    loading.value = false;
  }
};

const submitRegister = async () => {
  errorMsg.value = '';
  infoMsg.value = '';
  if (!form.username.trim() || form.password.length < 6) {
    errorMsg.value = '用户名不能为空,密码至少 6 位';
    return;
  }
  if (form.password !== form.password2) {
    errorMsg.value = '两次输入的密码不一致';
    return;
  }
  loading.value = true;
  try {
    const message = await auth.register(form.username.trim(), form.password,
      form.email.trim() || undefined);
    infoMsg.value = message;
    mode.value = 'login';
  } catch (e: any) {
    errorMsg.value = String(e?.response?.data?.detail ?? e?.message ?? '注册失败');
  } finally {
    loading.value = false;
  }
};
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-brand">
        <span class="brand-seal" aria-hidden="true">述</span>
        <div>
          <div class="brand-name">文献综述编辑部</div>
          <div class="brand-sub">Literature Review Press · 请登录后继续</div>
        </div>
      </div>

      <el-tabs v-model="mode" class="login-tabs">
        <el-tab-pane label="登录" name="login">
          <el-form label-position="top" @submit.prevent="submitLogin">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="用户名或邮箱" autofocus />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" show-password
                        @keyup.enter="submitLogin" />
            </el-form-item>
            <el-button type="primary" class="login-btn" :loading="loading" @click="submitLogin">
              登录
            </el-button>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form label-position="top" @submit.prevent="submitRegister">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="2-32 位中文/字母/数字/下划线" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" show-password
                        placeholder="至少 6 位" />
            </el-form-item>
            <el-form-item label="确认密码">
              <el-input v-model="form.password2" type="password" show-password
                        @keyup.enter="submitRegister" />
            </el-form-item>
            <el-form-item label="邮箱(可选)">
              <el-input v-model="form.email" placeholder="用于区分账号,可留空" />
            </el-form-item>
            <el-button type="primary" class="login-btn" :loading="loading" @click="submitRegister">
              提交注册申请
            </el-button>
            <div class="register-note">注册后需管理员在「监控与管理 → 用户管理」审核通过才能登录。</div>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <el-alert
        v-if="errorMsg"
        type="error"
        :closable="false"
        show-icon
        class="login-alert"
        :title="errorMsg"
      />
      <el-alert
        v-else-if="infoMsg"
        type="success"
        :closable="false"
        show-icon
        class="login-alert"
        :title="infoMsg"
      />
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: calc(100vh - 40px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.login-card {
  width: 420px;
  max-width: 100%;
  padding: 30px 34px 26px;
  background: var(--sheet);
  border: 1px solid var(--rule);
  border-top: 4px solid var(--cinnabar);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sheet);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 18px;
}

.brand-seal {
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--cinnabar);
  color: #fff;
  font-size: 20px;
  border-radius: 8px;
  flex: none;
}

.brand-name {
  font-size: 18px;
  color: var(--ink);
  letter-spacing: 0.06em;
}

.brand-sub {
  font-size: 12px;
  color: var(--ink-faint);
  margin-top: 2px;
}

.login-tabs {
  margin-top: 4px;
}

.login-btn {
  width: 100%;
  margin-top: 4px;
}

.login-alert {
  margin-top: 14px;
}

.register-note {
  margin-top: 10px;
  font-size: 12px;
  color: var(--ink-faint);
  line-height: 1.7;
}
</style>
