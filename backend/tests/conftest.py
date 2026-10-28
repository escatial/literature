# -*- coding: utf-8 -*-
"""后端测试全局配置。

v9.9 起写作主链路包含可选的 LLM 增强环节(主题综合 WRITING_SYNTHESIS、
支持性裁判 QA_SUPPORT_JUDGE)。单测必须确定性执行:
- WRITING_SYNTHESIS 默认关闭(专项测试自行 monkeypatch 为开并 mock LLM);
- QA_SUPPORT_JUDGE 本就默认关闭,这里显式声明防泄漏。
生产环境不设这些变量,增强环节保持默认行为(synthesis 开、judge 关)。
"""
import os

os.environ.setdefault("WRITING_SYNTHESIS", "0")
os.environ.setdefault("QA_SUPPORT_JUDGE", "0")
os.environ.setdefault("QA_SPOTCHECK", "0")  # 回源核验需外网,单测不打真实接口
# 自评闭环每章多发 1-3 次 LLM 调用;单测关闭(专项测试自行 monkeypatch 开关)
os.environ.setdefault("WRITING_SELF_EVAL", "0")

# LLM key 隔离:tests/crawler/integration/test_admin_api.py 导入 main.py 时,
# main 顶层的 load_dotenv() 会把 backend/.env 的真实 MINIMAX/DEEPSEEK/GPT key
# 永久写入 os.environ(dotenv 不受 monkeypatch 恢复)。此后任何走到真实
# messages_create 的测试都会打真实 API 经系统代理挂住几分钟(线上实测:
# crawler 先跑时 test_qa_soft_gate / wiring 测试全部卡死)。单测从不打真实
# LLM——这里显式清空 key,让 messages_create 快速失败;确需真实网络的白盒
# 测试应自行在用例内 setenv。
_LLM_KEY_ENVS = ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "GPT_API_KEY", "OPENAI_API_KEY")
for _key in _LLM_KEY_ENVS:
    os.environ.pop(_key, None)

import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_keys():
    """每个用例前强制清空 LLM key。

    conftest 导入期清一次不够:crawler 集成测试中途导入 main.py 触发
    load_dotenv(),key 会在此后的用例里复活。autouse 保证任意顺序下,
    单测的 messages_create 都因缺 key 快速失败,绝不打真实 API。
    tests/automation、tests/retrieval(联网白盒)如需真实 key,须绕过本
    fixture 直接运行。

    B2:登录限流在单测中默认关闭(AUTH_RATELIMIT_DISABLED=1),避免既有
    auth 流测试的失败重试触发锁定;限流专项测试用例内 monkeypatch.delenv
    显式开启,teardown 后恢复本关闭态。
    """
    for _key in _LLM_KEY_ENVS:
        os.environ.pop(_key, None)
    os.environ["AUTH_RATELIMIT_DISABLED"] = "1"
    yield
    os.environ.pop("AUTH_RATELIMIT_DISABLED", None)

# backend/src 与 backend 加入 import 路径(与各子目录 conftest 同构,兜底)
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

