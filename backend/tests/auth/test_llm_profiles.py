# -*- coding: utf-8 -*-
"""LLM 供应商配置(llm_profiles)端到端测试。

覆盖:
- 增删改查 + 列表脱敏(api_key 一律 mask_key,响应无明文);
- name 同用户重复 409(DB 唯一约束 + 应用层预查);
- activate 把 profile 字段写入 users 表 llm_* 激活字段(运行时零改动语义);
- 用户隔离:A/B 各自只能看到/操作自己的配置,越权一律 404(不泄露存在性);
- SSRF:base_url 指内网/环回 → 400;
- 鉴权:未登录 401;
- 探测端点(llm-config/models)双模块树 patch,不联网。

凭据/用户名全部运行时随机生成,源码无凭据字面量。
"""
import secrets

import pytest

import db.session as db_session
import db.models  # noqa: F401  必须先注册模型,create_all 才有表
from db.models import UserModel
from db.session import Base

# 运行时生成的测试 Key(非真实凭据;末 4 位用于掩码断言)
KEY_A = "sk-test-" + secrets.token_hex(10)
KEY_B = "sk-test-" + secrets.token_hex(10)
KEY_C = "sk-test-" + secrets.token_hex(10)


def _username(tag: str) -> str:
    """随机用户名(满足 ^[A-Za-z0-9_]{2,32}$)。"""
    return f"u_{tag}_{secrets.token_hex(4)}"


def _register_and_login(client, tag: str, admin_headers: dict | None = None) -> tuple[dict, str]:
    """注册并登录,返回 (鉴权头, user_id)。

    非首个注册用户 status=pending 需审核:传入 admin_headers 时由管理员
    审核通过后再登录(admin 用户的审核接口要求管理员角色)。
    """
    username = _username(tag)
    password = "pw-" + secrets.token_hex(8)
    r = client.post("/api/auth/register", json={
        "username": username, "password": password,
    })
    assert r.status_code == 200, r.text
    login = client.post("/api/auth/login", json={
        "username": username, "password": password,
    })
    if login.status_code == 403 and admin_headers is not None:
        users = client.get("/api/auth/admin/users", headers=admin_headers).json()["users"]
        uid = next(u["user_id"] for u in users if u["username"] == username)
        assert client.post(
            f"/api/auth/admin/users/{uid}/approve", headers=admin_headers,
        ).status_code == 200
        login = client.post("/api/auth/login", json={
            "username": username, "password": password,
        })
    assert login.status_code == 200, login.text
    body = login.json()
    return {"X-Auth-Token": body["token"]}, body["user"]["user_id"]


def _create(client, headers, *, name="配置A", kind="openai",
            base_url="https://llm.example.com/v1", api_key=KEY_A, model="m-1"):
    return client.post("/api/auth/llm-profiles", headers=headers, json={
        "name": name, "kind": kind, "base_url": base_url,
        "api_key": api_key, "model": model,
    })


# ---------- 鉴权 ----------

def test_profiles_require_login(client):
    assert client.get("/api/auth/llm-profiles").status_code == 401
    assert client.post("/api/auth/llm-profiles", json={}).status_code == 401
    assert client.delete("/api/auth/llm-profiles/1").status_code == 401
    assert client.post("/api/auth/llm-profiles/1/activate").status_code == 401


# ---------- 增查 + 脱敏 ----------

def test_create_and_list_masks_key(client):
    headers, _uid = _register_and_login(client, "a")
    r = _create(client, headers, name="MiniMax 国内")
    assert r.status_code == 200, r.text
    profile = r.json()["profile"]
    assert profile["name"] == "MiniMax 国内"
    assert profile["kind"] == "openai"
    assert profile["base_url"] == "https://llm.example.com/v1"
    assert profile["model"] == "m-1"
    assert profile["is_active"] is False  # 尚未激活

    r = client.get("/api/auth/llm-profiles", headers=headers)
    assert r.status_code == 200
    profiles = r.json()["profiles"]
    assert len(profiles) == 1
    # 脱敏断言:掩码格式正确,且全响应不出现明文 key
    assert profiles[0]["api_key_masked"] == "****" + KEY_A[-4:]
    assert KEY_A not in r.text


def test_create_validations(client):
    headers, _uid = _register_and_login(client, "v")
    # key 必填
    r = client.post("/api/auth/llm-profiles", headers=headers, json={
        "name": "n1", "kind": "openai", "base_url": "https://x.example.com",
        "api_key": "", "model": "",
    })
    assert r.status_code == 400
    # name 必填
    r = client.post("/api/auth/llm-profiles", headers=headers, json={
        "name": "  ", "kind": "openai", "base_url": "https://x.example.com",
        "api_key": KEY_A, "model": "",
    })
    assert r.status_code == 400
    # base_url 必填
    r = client.post("/api/auth/llm-profiles", headers=headers, json={
        "name": "n2", "kind": "openai", "base_url": "",
        "api_key": KEY_A, "model": "",
    })
    assert r.status_code == 400
    # kind 白名单
    r = client.post("/api/auth/llm-profiles", headers=headers, json={
        "name": "n3", "kind": "gemini", "base_url": "https://x.example.com",
        "api_key": KEY_A, "model": "",
    })
    assert r.status_code == 400


# ---------- name 唯一 ----------

def test_duplicate_name_409(client):
    headers, _uid = _register_and_login(client, "d")
    assert _create(client, headers, name="同名配置").status_code == 200
    r = _create(client, headers, name="同名配置")
    assert r.status_code == 409
    # 改名撞已有名字同样 409
    assert _create(client, headers, name="第二份").status_code == 200
    list_body = client.get("/api/auth/llm-profiles", headers=headers).json()
    second_id = list_body["profiles"][1]["profile_id"]
    r = client.put(f"/api/auth/llm-profiles/{second_id}", headers=headers, json={
        "name": "同名配置", "kind": "openai",
        "base_url": "https://llm.example.com/v1",
        "api_key": "", "model": "",
    })
    assert r.status_code == 409


# ---------- 编辑 / 删除 ----------

def test_update_keeps_key_when_blank(client):
    headers, _uid = _register_and_login(client, "u")
    pid = _create(client, headers).json()["profile"]["profile_id"]
    # api_key 留空 = 保持原 key;其余字段更新
    r = client.put(f"/api/auth/llm-profiles/{pid}", headers=headers, json={
        "name": "配置A-改", "kind": "anthropic",
        "base_url": "https://api.anthropic.example.com",
        "api_key": "", "model": "claude-x",
    })
    assert r.status_code == 200
    profile = r.json()["profile"]
    assert profile["name"] == "配置A-改"
    assert profile["kind"] == "anthropic"
    assert profile["base_url"] == "https://api.anthropic.example.com"
    assert profile["model"] == "claude-x"
    assert profile["api_key_masked"] == "****" + KEY_A[-4:]  # key 未变
    # 提交新 key 则更新
    r = client.put(f"/api/auth/llm-profiles/{pid}", headers=headers, json={
        "name": "配置A-改", "kind": "anthropic",
        "base_url": "https://api.anthropic.example.com",
        "api_key": KEY_C, "model": "claude-x",
    })
    assert r.status_code == 200
    assert r.json()["profile"]["api_key_masked"] == "****" + KEY_C[-4:]
    assert KEY_C not in r.text


def test_delete_then_empty(client):
    headers, _uid = _register_and_login(client, "x")
    pid = _create(client, headers).json()["profile"]["profile_id"]
    assert client.delete(f"/api/auth/llm-profiles/{pid}", headers=headers).json()["ok"] is True
    assert client.get("/api/auth/llm-profiles", headers=headers).json()["profiles"] == []
    # 删除已不存在的 → 404
    assert client.delete(f"/api/auth/llm-profiles/{pid}", headers=headers).status_code == 404


# ---------- activate:写入 users 激活字段 ----------

def test_activate_writes_user_llm_fields(client):
    headers, uid = _register_and_login(client, "act")
    base_url = "https://anthropic-relay.example.com"
    pid = _create(
        client, headers, name="官方 Anthropic", kind="anthropic",
        base_url=base_url, api_key=KEY_B, model="claude-y",
    ).json()["profile"]["profile_id"]

    r = client.post(f"/api/auth/llm-profiles/{pid}/activate", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["is_active"] is True
    assert r.json()["user"]["llm_base_url"] == base_url
    assert r.json()["user"]["llm_api_key_masked"] == "****" + KEY_B[-4:]

    # users 表逐字段断言(切换 = 写入既有激活字段)
    with db_session.SessionLocal() as db:
        row = db.query(UserModel).filter_by(user_id=uid).one()
        assert row.llm_kind == "anthropic"
        assert row.llm_base_url == base_url
        assert row.llm_api_key == KEY_B
        assert row.llm_model == "claude-y"

    # 列表激活态:激活项 True,未激活项 False
    profiles = client.get("/api/auth/llm-profiles", headers=headers).json()["profiles"]
    by_id = {p["profile_id"]: p for p in profiles}
    assert by_id[pid]["is_active"] is True
    pid2 = _create(client, headers, name="第二份").json()["profile"]["profile_id"]
    profiles = client.get("/api/auth/llm-profiles", headers=headers).json()["profiles"]
    by_id = {p["profile_id"]: p for p in profiles}
    assert by_id[pid2]["is_active"] is False
    # 切到第二份后,第一份自动失活
    client.post(f"/api/auth/llm-profiles/{pid2}/activate", headers=headers)
    profiles = client.get("/api/auth/llm-profiles", headers=headers).json()["profiles"]
    by_id = {p["profile_id"]: p for p in profiles}
    assert by_id[pid]["is_active"] is False
    assert by_id[pid2]["is_active"] is True


# ---------- 用户隔离 ----------

def test_user_isolation_across_users(client):
    headers_a, _ = _register_and_login(client, "isoa")  # 首个注册者 = 管理员
    headers_b, _ = _register_and_login(client, "isob", admin_headers=headers_a)
    pid_a = _create(client, headers_a, name="A的配置").json()["profile"]["profile_id"]

    # B 看不到 A 的配置
    profiles_b = client.get("/api/auth/llm-profiles", headers=headers_b).json()["profiles"]
    assert all(p["profile_id"] != pid_a for p in profiles_b)
    # B 改/删/激活 A 的 → 404(不泄露存在性)
    assert client.put(f"/api/auth/llm-profiles/{pid_a}", headers=headers_b, json={
        "name": "抢改名", "kind": "openai",
        "base_url": "https://llm.example.com/v1", "api_key": KEY_A, "model": "",
    }).status_code == 404
    assert client.delete(f"/api/auth/llm-profiles/{pid_a}", headers=headers_b).status_code == 404
    assert client.post(f"/api/auth/llm-profiles/{pid_a}/activate", headers=headers_b).status_code == 404
    # A 的数据原封不动
    profiles_a = client.get("/api/auth/llm-profiles", headers=headers_a).json()["profiles"]
    assert len(profiles_a) == 1
    assert profiles_a[0]["name"] == "A的配置"


# ---------- SSRF ----------

@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:9000/v1",
    "http://192.168.1.10:8000/v1",
    "http://10.0.0.5/v1",
    "http://localhost:8000",
    "http://169.254.169.254/latest",
])
def test_ssrf_rejected_on_create_and_update(client, bad_url):
    headers, _uid = _register_and_login(client, "ssrf")
    r = _create(client, headers, name="内网", base_url=bad_url)
    assert r.status_code == 400
    pid = _create(client, headers).json()["profile"]["profile_id"]
    r = client.put(f"/api/auth/llm-profiles/{pid}", headers=headers, json={
        "name": "配置A", "kind": "openai", "base_url": bad_url,
        "api_key": "", "model": "",
    })
    assert r.status_code == 400


# ---------- 探测端点(mock,不联网;双模块树 patch) ----------

def test_probe_models_with_profile_key_mocked(client, monkeypatch):
    """表单留空 key 时回落已激活配置的 key;fetch_models_for 打桩不联网。

    双模块树 patch:api.auth 的 from-import 引用 + llm.client 定义处,
    任一路径调用都命中桩。
    """
    import api.auth as api_auth
    from llm import client as llm_client

    calls: list[tuple[str, str, str]] = []

    def _fake_fetch(kind: str, base_url: str, api_key: str) -> list[dict]:
        calls.append((kind, base_url, api_key))
        return [{"id": "mock-model-1"}, {"id": "mock-model-2"}]

    monkeypatch.setattr(api_auth, "fetch_models_for", _fake_fetch)
    monkeypatch.setattr(llm_client, "fetch_models_for", _fake_fetch)

    headers, _uid = _register_and_login(client, "probe")
    _create(client, headers, name="探测源", api_key=KEY_A,
            base_url="https://llm.example.com/v1", model="")
    pid = client.get("/api/auth/llm-profiles", headers=headers).json()["profiles"][0]["profile_id"]
    client.post(f"/api/auth/llm-profiles/{pid}/activate", headers=headers)

    # 表单只填 base_url,key 留空 → 用已激活配置的 key
    r = client.post("/api/auth/llm-config/models", headers=headers, json={
        "llm_kind": "openai",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": "",
    })
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["models"]] == ["mock-model-1", "mock-model-2"]
    assert calls and calls[0][2] == KEY_A  # 桩收到的正是激活配置的 key
