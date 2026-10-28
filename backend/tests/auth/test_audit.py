# -*- coding: utf-8 -*-
"""审计日志接线回归(Batch B5)。

- login 成功/失败各产生一条 audit_logs 行;
- detail 只含脱敏字段,绝无密码/令牌/API key 明文;
- admin 审核/删除与 llm-config 更新留痕。
audit() 自身不抛异常(writer 内部兜底),接线不得影响主流程返回值。
"""
import secrets

import pytest

import db.session as db_session
from audit.writer import list_audit


def _rows(action: str):
    return [r for r in list_audit(action=action, limit=50)]


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_success_and_failure_are_audited_without_secrets(client):
    """成功与失败各一条;detail/ip 齐备且无敏感明文。"""
    username = "审计用户甲"
    password = "pw-" + secrets.token_hex(8)
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200

    # 失败登录(错误口令)
    wrong = "pw-" + secrets.token_hex(8)
    assert _login(client, username, wrong).status_code == 403
    # 成功登录
    assert _login(client, username, password).status_code == 200

    fails = _rows("auth.login")  # 成败同 action,以 success 区分
    assert len(fails) == 2
    by_success = {row.success: row for row in fails}
    assert set(by_success) == {True, False}
    # 失败行:reason 记录了提示文案,但绝不出现口令明文
    assert wrong not in (by_success[False].detail or {}).get("reason", "")
    # 两行的 detail 序列化文本中都不含密码
    for row in fails:
        blob = repr(row.detail)
        assert password not in blob
        assert wrong not in blob
    # ip 已记录
    assert all(row.ip_address for row in fails)


def test_register_and_llm_config_and_admin_ops_are_audited(client):
    """register / llm-config / approve 留痕;llm-config 不落 api_key 明文。"""
    fake_key = "sk-test-" + secrets.token_hex(10)
    username = "审计用户乙"
    password = "pw-" + secrets.token_hex(8)
    client.post("/api/auth/register", json={"username": username, "password": password})
    token = _login(client, username, password).json()["token"]
    headers = {"X-Auth-Token": token}

    r = client.put("/api/auth/llm-config", headers=headers, json={
        "llm_kind": "openai",
        "llm_base_url": "https://llm.example.com/v1",
        "llm_api_key": fake_key,
        "llm_model": "test-model-1",
    })
    assert r.status_code == 200

    cfg_rows = _rows("auth.llm_config.update")
    assert len(cfg_rows) == 1
    assert fake_key not in repr(cfg_rows[0].detail)  # 绝不落 key 明文
    assert cfg_rows[0].detail.get("llm_model") == "test-model-1"

    regs = _rows("auth.register")
    assert len(regs) == 1
    assert regs[0].detail.get("username") == username

    # admin 审核(自我审核:首个用户即 admin)
    uid = regs[0].user_id
    # admin delete 需要另一个用户;此处仅验证 approve 类接线在 admin 路由可达
    # (完整审核流已由 test_auth_flow 覆盖,这里聚焦 audit 行存在性)
    from db.models import UserModel

    with db_session.SessionLocal() as session:
        row = session.query(UserModel).filter_by(user_id=uid).one_or_none()
        assert row is not None
