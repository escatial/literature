# -*- coding: utf-8 -*-
"""认证 / 注册审核 / 按用户 LLM 配置 的端到端测试。

- 独立临时 sqlite(reset_engine),不碰真实 lit_review.db;
- 走 main.app 全量中间件链(鉴权 401 / 公开路径放行 / state.user 注入);
- LLM 覆盖用假 client 验证 resolve_provider/select_providers 生效,
  绝不打真实 API(conftest 已清空全部 key);
- 口令/Key 一律运行时随机生成,源码中不出现凭据字面量。
"""
import secrets

import pytest
from fastapi.testclient import TestClient

import db.session as db_session
import db.models  # noqa: F401  必须先注册模型,create_all 才有表
from db.session import Base

# 运行时生成的测试口令/Key(每次运行不同,非真实凭据)
ADMIN_PW = "pw-" + secrets.token_hex(8)
USER_PW = "pw-" + secrets.token_hex(8)
FAKE_KEY = "sk-test-" + secrets.token_hex(10)
FAKE_KEY_A = "sk-test-" + secrets.token_hex(10)
FAKE_KEY_B = "sk-test-" + secrets.token_hex(10)
EPHEMERAL_KEY = "sk-" + secrets.token_hex(12)


@pytest.fixture()
def client(tmp_path):
    """独立临时库 + main.app(含鉴权中间件);不触发 lifespan(init_db)。"""
    url = f"sqlite:///{tmp_path / 'auth_test.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    from llm import client as llm_client

    llm_client.clear_runtime_llm()
    llm_client.set_model_override(None)
    import main

    yield TestClient(main.app)
    llm_client.clear_runtime_llm()
    llm_client.set_model_override(None)
    db_session.engine.dispose()


def _register(client, username, password):
    return client.post("/api/auth/register", json={
        "username": username, "password": password,
    })


def _login(client, username, password):
    return client.post("/api/auth/login", json={
        "username": username, "password": password,
    })


def _auth(token):
    return {"X-Auth-Token": token}


# ---------- bootstrap / 首个用户 = 管理员 ----------

def test_bootstrap_empty(client):
    r = client.get("/api/auth/bootstrap")
    assert r.status_code == 200
    assert r.json() == {"has_users": False}


def test_default_admin_seeded_once_and_login(client):
    """启动 seed:空库预置 admin/admin,幂等不覆盖,可直接登录。"""
    from security.auth import ensure_default_admin

    assert ensure_default_admin() is True
    assert ensure_default_admin() is False  # 已有用户,绝不重复创建/改密
    r = _login(client, "admin", "admin")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["status"] == "approved"
    # seed 的管理员注册同名会被拒(不撞车)
    assert _register(client, "admin", "pw-" + secrets.token_hex(8)).status_code == 400


def test_first_user_becomes_admin_and_can_login(client):
    assert _register(client, "管理员甲", ADMIN_PW).json()["status"] == "approved"
    r = _login(client, "管理员甲", ADMIN_PW)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["status"] == "approved"
    assert len(body["token"]) >= 32


# ---------- 注册审核流 ----------

def test_second_user_pending_then_admin_approves(client):
    _register(client, "管理员甲", ADMIN_PW)
    admin_token = _login(client, "管理员甲", ADMIN_PW).json()["token"]

    r = _register(client, "普通用户乙", USER_PW)
    assert r.json()["status"] == "pending"
    # 未审核不能登录
    assert _login(client, "普通用户乙", USER_PW).status_code == 403

    users = client.get("/api/auth/admin/users", headers=_auth(admin_token)).json()["users"]
    pending_id = next(u["user_id"] for u in users if u["username"] == "普通用户乙")
    assert client.post(f"/api/auth/admin/users/{pending_id}/approve",
                       headers=_auth(admin_token)).status_code == 200
    assert _login(client, "普通用户乙", USER_PW).status_code == 200


def test_rejected_user_cannot_login(client):
    _register(client, "管理员甲", ADMIN_PW)
    admin_token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    _register(client, "路人丙", USER_PW)
    users = client.get("/api/auth/admin/users", headers=_auth(admin_token)).json()["users"]
    uid = next(u["user_id"] for u in users if u["username"] == "路人丙")
    client.post(f"/api/auth/admin/users/{uid}/reject", headers=_auth(admin_token))
    r = _login(client, "路人丙", USER_PW)
    assert r.status_code == 403
    assert "拒绝" in r.json()["detail"]


def test_non_admin_cannot_manage_users(client):
    _register(client, "管理员甲", ADMIN_PW)
    _register(client, "普通用户乙", USER_PW)
    admin_token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    users = client.get("/api/auth/admin/users", headers=_auth(admin_token)).json()["users"]
    uid = next(u["user_id"] for u in users if u["username"] == "普通用户乙")
    client.post(f"/api/auth/admin/users/{uid}/approve", headers=_auth(admin_token))
    user_token = _login(client, "普通用户乙", USER_PW).json()["token"]
    assert client.get("/api/auth/admin/users", headers=_auth(user_token)).status_code == 403


# ---------- 鉴权中间件 ----------

def test_protected_routes_require_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/papers").status_code == 401
    assert client.get("/api/healthz").status_code == 200  # 探活公开
    assert client.get("/").status_code == 200            # 根路径公开


def test_me_with_valid_token(client):
    _register(client, "管理员甲", ADMIN_PW)
    token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    r = client.get("/api/auth/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "管理员甲"
    assert "password_hash" not in r.json()["user"]
    # 注销后令牌失效
    client.post("/api/auth/logout", headers=_auth(token))
    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401


def test_duplicate_username_rejected(client):
    _register(client, "管理员甲", ADMIN_PW)
    r = _register(client, "管理员甲", "pw-" + secrets.token_hex(8))
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]


# ---------- 按用户 LLM 配置 ----------

def test_llm_config_save_and_masked_roundtrip(client):
    _register(client, "管理员甲", ADMIN_PW)
    token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    r = client.put("/api/auth/llm-config", headers=_auth(token), json={
        "llm_kind": "openai",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": FAKE_KEY,
        "llm_model": "test-model-1",
    })
    assert r.status_code == 200
    user = r.json()["user"]
    assert user["llm_base_url"] == "https://llm.example.com/v1"
    # 响应必须脱敏:只允许掩码形态,绝不回传完整 key
    assert user["llm_api_key_masked"] == f"****{FAKE_KEY[-4:]}"
    assert FAKE_KEY not in r.text
    # 留空 key = 保持不变
    r2 = client.put("/api/auth/llm-config", headers=_auth(token), json={
        "llm_kind": "openai",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": "",
        "llm_model": "test-model-2",
    })
    assert r2.json()["user"]["llm_api_key_masked"] == f"****{FAKE_KEY[-4:]}"


def test_runtime_llm_override_drives_provider_resolution(client, monkeypatch):
    from llm import client as llm_client

    # fixture 里 import main 会 load_dotenv 把真实 key 写回 env;
    # 「无 env key/model」的前提需显式清掉(monkeypatch 测试后自动恢复)
    for k in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "GPT_API_KEY",
              "OPENAI_API_KEY", "MINIMAX_MODEL", "MINIMAX_BASE_URL"):
        monkeypatch.delenv(k, raising=False)

    # 无 env key、无覆盖 → 不可用
    llm_client.clear_runtime_llm()
    with pytest.raises(RuntimeError):
        llm_client.select_providers(None)

    # 模拟用户配置应用(apply_user_llm 保存后的状态)
    llm_client.set_runtime_llm(
        kind="openai",
        base_url="https://llm.example.com/v1",
        api_key=FAKE_KEY,
        model="test-model-1",
    )
    assert llm_client.select_providers(None) == (llm_client.get_default_provider(),)
    rp = llm_client.resolve_provider("minimax")
    assert rp.base_url == "https://llm.example.com/v1"
    assert rp.api_key == FAKE_KEY
    assert rp.model == "test-model-1"
    assert rp.id == "openai" and rp.mode == "chat"  # 不注入 reasoning_split

    # 历史 kind "minimax" 归一化为 openai(MiniMax 端点本就是 OpenAI 兼容)
    llm_client.set_runtime_llm(
        kind="minimax", base_url="https://llm.example.com/v1",
        api_key=FAKE_KEY, model="MiniMax-M3",
    )
    rp = llm_client.resolve_provider("minimax")
    assert rp.id == "openai" and rp.mode == "chat"

    # Anthropic 兼容协议 → mode=anthropic
    llm_client.set_runtime_llm(
        kind="anthropic", base_url="https://api.anthropic.com",
        api_key=FAKE_KEY, model="claude-sonnet-4-5",
    )
    rp = llm_client.resolve_provider("minimax")
    assert rp.id == "anthropic" and rp.mode == "anthropic"

    # 覆盖留空模型且 env 也没有 → 明确报错
    llm_client.set_runtime_llm(
        kind="openai", base_url="https://x.example.com/v1",
        api_key=EPHEMERAL_KEY, model="",
    )
    with pytest.raises(RuntimeError, match="模型"):
        llm_client.resolve_provider("minimax")

    # 清除覆盖后回落 env(无 key → 报缺 key)
    llm_client.clear_runtime_llm()
    with pytest.raises(RuntimeError):
        llm_client.select_providers(None)


def test_anthropic_url_and_sse_parsing():
    """Anthropic 协议:URL 拼接与 SSE 增量解析(纯函数,零网络)。"""
    from llm.client import (
        _anthropic_parse_sse_line,
        _anthropic_text,
        _anthropic_thinking,
        _anthropic_url,
    )

    # base 带 /v1 与不带 /v1 两种形态
    assert _anthropic_url("https://api.anthropic.com", "messages") == \
        "https://api.anthropic.com/v1/messages"
    assert _anthropic_url("https://api.minimax.cn/anthropic/", "messages") == \
        "https://api.minimax.cn/anthropic/v1/messages"
    assert _anthropic_url("https://gw.example.com/v1", "messages") == \
        "https://gw.example.com/v1/messages"

    # SSE:thinking → reasoning,text → content,其余忽略,error 抛出
    assert _anthropic_parse_sse_line(
        'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"我在想"}}'
    ) == ("reasoning", "我在想")
    assert _anthropic_parse_sse_line(
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"正文"}}'
    ) == ("content", "正文")
    assert _anthropic_parse_sse_line("event: content_block_delta") is None
    assert _anthropic_parse_sse_line('data: {"type":"message_stop"}') is None
    assert _anthropic_parse_sse_line("data: [DONE]") is None
    with pytest.raises(RuntimeError, match="流式错误"):
        _anthropic_parse_sse_line(
            'data: {"type":"error","error":{"message":"overloaded"}}'
        )

    # 响应 text/thinking 块提取
    data = {"content": [
        {"type": "thinking", "thinking": "思路"},
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]}
    assert _anthropic_text(data) == "第一段第二段"
    assert _anthropic_thinking(data) == "思路"


def test_llm_config_rejects_private_urls(client):
    """SSRF 防护:保存/探测的自定义 URL 拒绝本机/内网/保留地址与非 http(s)。"""
    _register(client, "管理员甲", ADMIN_PW)
    token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    for bad in (
        "http://127.0.0.1:8000/v1",
        "http://localhost:11434/v1",
        "http://192.168.1.9/v1",
        "http://10.0.0.5/v1",
        "http://169.254.169.254/latest",
        "ftp://llm.example.com/v1",
        "http://box.local/v1",
    ):
        r = client.put("/api/auth/llm-config", headers=_auth(token), json={
            "llm_kind": "openai", "llm_base_url": bad,
            "llm_api_key": FAKE_KEY, "llm_model": "m",
        })
        assert r.status_code == 400, f"{bad} 应被拒绝,实际 {r.status_code}"
    # 公网地址正常保存
    ok = client.put("/api/auth/llm-config", headers=_auth(token), json={
        "llm_kind": "openai", "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": FAKE_KEY, "llm_model": "m",
    })
    assert ok.status_code == 200


def test_models_upstream_and_select_user_scope(client, monkeypatch):
    """已配置用户:模型探测/切换作用于自己的配置(select 落库 llm_model)。"""
    from llm import client as llm_client
    import src.llm.client as src_llm  # review.py 引用的是 src 树,双树都要打桩

    fake_models = [
        {"id": "user-model-a", "owned_by": "", "created": 0},
        {"id": "user-model-b", "owned_by": "", "created": 0},
    ]
    for tree in (llm_client, src_llm):
        monkeypatch.setattr(
            tree, "fetch_models_for",
            lambda kind, base, key, timeout=15.0: [
                dict(m) for m in fake_models
            ],
        )
    _register(client, "管理员甲", ADMIN_PW)
    token = _login(client, "管理员甲", ADMIN_PW).json()["token"]
    assert client.put("/api/auth/llm-config", headers=_auth(token), json={
        "llm_kind": "openai", "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": FAKE_KEY, "llm_model": "user-model-a",
    }).status_code == 200

    # upstream:source=user,active=用户当前模型
    r = client.get("/api/review/models/upstream", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["user_configured"] is True and body["source"] == "user"
    assert body["active"] == "user-model-a"
    assert {m["id"] for m in body["models"]} == {"user-model-a", "user-model-b"}

    # select 用户级:切到 b 后 llm_model 落库
    r = client.post("/api/review/models/select", headers=_auth(token),
                    json={"model_id": "user-model-b"})
    assert r.status_code == 200 and r.json()["scope"] == "user"
    me = client.get("/api/auth/me", headers=_auth(token)).json()["user"]
    assert me["llm_model"] == "user-model-b"
    # 不在上游列表中的模型被拒
    assert client.post("/api/review/models/select", headers=_auth(token),
                       json={"model_id": "nope"}).status_code == 400

    # 未配置用户:source=system(user_configured=False)
    client.post(f"/api/auth/admin/users", headers=_auth(token))
    _register(client, "普通用户乙", USER_PW)
    admin_users = client.get("/api/auth/admin/users", headers=_auth(token)).json()["users"]
    uid = next(u["user_id"] for u in admin_users if u["username"] == "普通用户乙")
    client.post(f"/api/auth/admin/users/{uid}/approve", headers=_auth(token))
    user_token = _login(client, "普通用户乙", USER_PW).json()["token"]
    body2 = client.get("/api/review/models/upstream", headers=_auth(user_token)).json()
    assert body2["user_configured"] is False and body2["source"] == "system"


def test_client_cache_distinguishes_override_targets(client):
    """同一 provider id 下切换 base_url/key,客户端缓存必须分开。"""
    from llm import client as llm_client

    llm_client.set_runtime_llm(
        kind="openai", base_url="https://a.example.com/v1",
        api_key=FAKE_KEY_A, model="m1",
    )
    ca = llm_client.get_client("minimax", timeout=30.0)
    llm_client.set_runtime_llm(
        kind="openai", base_url="https://b.example.com/v1",
        api_key=FAKE_KEY_B, model="m2",
    )
    cb = llm_client.get_client("minimax", timeout=30.0)
    assert ca is not cb
    assert str(cb.base_url).rstrip("/") == "https://b.example.com/v1"


# ---------- 令牌过期 + 滑动续期(B1) ----------

def _naive_utc_now():
    """SQLite DATETIME 列无时区:读写统一 naive UTC,避免 aware/naive 混比。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _insert_token_row(user_id: str, expires_at):
    """绕过 issue_token 直接插入指定过期时间的令牌行,返回令牌原文。"""
    from db.models import AuthTokenModel
    from security.auth import _hash_token

    raw = "tok-" + secrets.token_hex(16)
    with db_session.SessionLocal() as db:
        db.add(AuthTokenModel(
            token_hash=_hash_token(raw), user_id=user_id, expires_at=expires_at,
        ))
        db.commit()
    return raw


def _first_user_id() -> str:
    from db.models import UserModel

    with db_session.SessionLocal() as session:
        return session.query(UserModel).order_by(
            UserModel.created_at.asc()
        ).first().user_id


def test_legacy_null_expiry_token_backfilled_and_usable(client):
    """存量令牌(expires_at=NULL):首次校验回填 TTL,不踢存量会话。"""
    from db.models import AuthTokenModel
    from security import auth as sauth

    _register(client, "管理员甲", ADMIN_PW)
    uid = _first_user_id()
    raw = _insert_token_row(uid, None)

    user = sauth.user_from_token(raw)
    assert user is not None and user.user_id == uid
    # 回填落库:非 NULL 且在未来一个 TTL 内
    with db_session.SessionLocal() as db:
        row = db.query(AuthTokenModel).filter_by(
            token_hash=sauth._hash_token(raw)
        ).one_or_none()
    assert row is not None
    assert row.expires_at is not None
    remaining = (row.expires_at - _naive_utc_now()).total_seconds()
    assert 0 < remaining <= sauth.AUTH_TOKEN_TTL_SECONDS
    # 回填后 HTTP 链路同样可用
    assert client.get("/api/auth/me", headers=_auth(raw)).status_code == 200


def test_expired_token_rejected_and_row_deleted(client):
    """过期令牌:HTTP 401 且行被删除(不残留死行)。"""
    from datetime import timedelta

    from db.models import AuthTokenModel
    from security import auth as sauth

    _register(client, "管理员甲", ADMIN_PW)
    uid = _first_user_id()
    raw = _insert_token_row(uid, _naive_utc_now() - timedelta(seconds=10))

    assert client.get("/api/auth/me", headers=_auth(raw)).status_code == 401
    with db_session.SessionLocal() as db:
        assert db.query(AuthTokenModel).filter_by(
            token_hash=sauth._hash_token(raw)
        ).one_or_none() is None


def test_token_sliding_renewal_extends_near_expiry(client):
    """剩余 < TTL/2:校验通过的同时顺延一个整 TTL(滑动续期)。"""
    from datetime import timedelta

    from db.models import AuthTokenModel
    from security import auth as sauth

    _register(client, "管理员甲", ADMIN_PW)
    uid = _first_user_id()
    # 剩余 1/4 TTL(< 半程) → 应被顺延
    raw = _insert_token_row(
        uid, _naive_utc_now() + timedelta(seconds=sauth.AUTH_TOKEN_TTL_SECONDS / 4)
    )

    assert sauth.user_from_token(raw) is not None
    with db_session.SessionLocal() as db:
        row = db.query(AuthTokenModel).filter_by(
            token_hash=sauth._hash_token(raw)
        ).one_or_none()
    remaining = (row.expires_at - _naive_utc_now()).total_seconds()
    # 顺延后剩余应超过半程(接近整 TTL),证明发生了续期
    assert remaining > sauth.AUTH_TOKEN_TTL_SECONDS / 2


def test_fresh_token_not_renewed_early(client):
    """剩余 > TTL/2 的新令牌:不得提前回写(写放大减半的另一半)。"""
    from datetime import timedelta

    from db.models import AuthTokenModel
    from security import auth as sauth

    _register(client, "管理员甲", ADMIN_PW)
    uid = _first_user_id()
    expiry = _naive_utc_now() + timedelta(seconds=sauth.AUTH_TOKEN_TTL_SECONDS - 60)
    raw = _insert_token_row(uid, expiry)

    assert sauth.user_from_token(raw) is not None
    with db_session.SessionLocal() as db:
        row = db.query(AuthTokenModel).filter_by(
            token_hash=sauth._hash_token(raw)
        ).one_or_none()
    assert row.expires_at == expiry  # 未被顺延
