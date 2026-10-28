# -*- coding: utf-8 -*-
"""登录失败限流回归(Batch B2)。

- 连续失败 ≥5 次/10 分钟 → 锁定,锁定窗口内**正确密码也 429**;
- 成功登录清零失败计数;
- 锁定窗口过期自动恢复;
- conftest autouse 默认 AUTH_RATELIMIT_DISABLED=1,本文件用例内显式开启。
"""
import secrets

import pytest

import security.ratelimit as ratelimit


@pytest.fixture()
def limiter_on(monkeypatch):
    """开启限流并清理计数;测试后 monkeypatch 自动恢复 conftest 的关闭态。"""
    monkeypatch.delenv("AUTH_RATELIMIT_DISABLED", raising=False)
    ratelimit.reset()
    yield
    ratelimit.reset()


def _register(client, username):
    r = client.post("/api/auth/register", json={"username": username, "password": "pw-" + secrets.token_hex(8)})
    assert r.status_code == 200


def test_fifth_failure_locks_even_correct_password(client, limiter_on):
    """连续 5 次错误密码 → 第 6 次一律 429。"""
    username = "限流用户甲"
    _register(client, username)
    wrong = "pw-" + secrets.token_hex(8)
    for _ in range(ratelimit.FAILURE_THRESHOLD):
        r = client.post("/api/auth/login", json={"username": username, "password": wrong})
        assert r.status_code == 403
    r = client.post("/api/auth/login", json={"username": username, "password": wrong})
    assert r.status_code == 429
    assert "锁定" in r.json()["detail"] or "重试" in r.json()["detail"]


def test_lockout_blocks_correct_password_within_window(client, limiter_on):
    """锁定窗口内:哪怕拿到正确密码也 429。"""
    username = "限流用户乙"
    password = "pw-" + secrets.token_hex(8)
    _register(client, username)
    wrong = "pw-" + secrets.token_hex(8)
    for _ in range(ratelimit.FAILURE_THRESHOLD):
        client.post("/api/auth/login", json={"username": username, "password": wrong})
    # 造一个真实可用凭据(直接改库绕过登录),证明 429 与密码对错无关
    from security import auth as sauth

    with sauth.db_session.SessionLocal() as db:
        from db.models import UserModel

        row = db.query(UserModel).filter_by(username=username).one_or_none()
        assert row is not None
        row.password_hash = sauth.hash_password(password)
        db.commit()
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 429


def test_lock_expires_and_recovers(client, limiter_on):
    """锁定窗口拨回过去(白盒)后:同键登录恢复正常(403 语义回归)。"""
    username = "限流用户丙"
    _register(client, username)
    wrong = "pw-" + secrets.token_hex(8)
    for _ in range(ratelimit.FAILURE_THRESHOLD):
        client.post("/api/auth/login", json={"username": username, "password": wrong})
    assert client.post(
        "/api/auth/login", json={"username": username, "password": wrong}
    ).status_code == 429

    # 白盒:把锁定拨到过去,模拟 15 分钟窗口走完
    import time as _time

    for key in list(ratelimit._records):
        ratelimit._records[key]["lock_until"] = _time.monotonic() - 1
    r = client.post("/api/auth/login", json={"username": username, "password": wrong})
    assert r.status_code == 403  # 恢复正常的"凭据错误"语义


def test_success_resets_failure_counter(client, limiter_on):
    """错误 4 次 + 成功清零 → 再错 4 次仍不锁(计数不跨成功累积)。"""
    username = "限流用户丁"
    password = "pw-" + secrets.token_hex(8)
    # 注册即 approved(首个用户),直接登录成功
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    assert r.json()["status"] == "approved"
    wrong = "pw-" + secrets.token_hex(8)
    for _ in range(ratelimit.FAILURE_THRESHOLD - 1):
        client.post("/api/auth/login", json={"username": username, "password": wrong})
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200  # 成功清零
    for _ in range(ratelimit.FAILURE_THRESHOLD - 1):
        client.post("/api/auth/login", json={"username": username, "password": wrong})
    # 未达阈值:仍然是凭据错误(403)而非锁定(429)
    assert client.post(
        "/api/auth/login", json={"username": username, "password": wrong}
    ).status_code == 403


def test_disabled_env_bypasses_limiter(client):
    """AUTH_RATELIMIT_DISABLED=1(全局默认):失败任意多次都保持 403,不 429。"""
    username = "限流关闭用户"
    _register(client, username)
    wrong = "pw-" + secrets.token_hex(8)
    for _ in range(ratelimit.FAILURE_THRESHOLD + 2):
        r = client.post("/api/auth/login", json={"username": username, "password": wrong})
        assert r.status_code == 403
