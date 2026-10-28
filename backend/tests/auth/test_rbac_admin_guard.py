# -*- coding: utf-8 -*-
"""crawler 管理面 RBAC 收权回归(Batch B4)。

边界说明(B4/B6 分工):crawler_admin 是运维管理面,整面收权 admin;
业务数据面(papers/检索任务)不做角色收权,靠 owner 归属过滤(B6)隔离。

- 管理面:/api/crawler/* 仅 admin 可用,普通用户 403,未登录 401;
- 未登录 401 由 main 鉴权中间件保证(与 /api/auth/me 同语义)。
"""
import secrets

import pytest


def _login_only(client, username, password) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.json()
    return r.json()["token"]


def test_anonymous_cannot_reach_crawler_admin(client):
    """未登录访问管理面:401(main 中间件统一拦截)。"""
    assert client.get("/api/crawler/dashboard").status_code == 401


def test_regular_user_gets_403_on_crawler_admin(client):
    """普通用户(首个 admin 之后注册的 pending→approved)访问管理面:403。"""
    # 首个注册者成为 admin
    admin_pw = "pw-" + secrets.token_hex(8)
    client.post("/api/auth/register", json={"username": "管理员甲", "password": admin_pw})
    # 第二个用户默认 pending,须 admin 审核后才可登录
    user_pw = "pw-" + secrets.token_hex(8)
    client.post("/api/auth/register", json={"username": "普通用户乙", "password": user_pw})
    r = client.post("/api/auth/login", json={"username": "管理员甲", "password": admin_pw})
    admin_token = r.json()["token"]
    users = client.get("/api/auth/admin/users", headers={"X-Auth-Token": admin_token}).json()["users"]
    uid = next(u["user_id"] for u in users if u["username"] == "普通用户乙")
    client.post(f"/api/auth/admin/users/{uid}/approve", headers={"X-Auth-Token": admin_token})

    user_token = _login_only(client, "普通用户乙", user_pw)
    r = client.get("/api/crawler/dashboard", headers={"X-Auth-Token": user_token})
    assert r.status_code == 403
    # 写操作面同样 403
    r2 = client.post("/api/crawler/tasks/stop-all", headers={"X-Auth-Token": user_token})
    assert r2.status_code == 403


def test_admin_can_reach_crawler_admin(client):
    """admin 可访问管理面(冒烟:鉴权链不误伤管理员)。"""
    admin_pw = "pw-" + secrets.token_hex(8)
    client.post("/api/auth/register", json={"username": "管理员甲", "password": admin_pw})
    r = client.post("/api/auth/login", json={"username": "管理员甲", "password": admin_pw})
    token = r.json()["token"]
    resp = client.get("/api/crawler/dashboard", headers={"X-Auth-Token": token})
    assert resp.status_code == 200
