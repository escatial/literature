# -*- coding: utf-8 -*-
"""auth 域测试共享 fixture。

client:独立临时 sqlite(reset_engine) + main.app 全量中间件链,
与 test_auth_flow.py 内的同名 fixture 同构(该文件保留自身定义,
就近优先不冲突);新增测试文件直接使用本 fixture。
"""
import secrets

import pytest
from fastapi.testclient import TestClient

import db.session as db_session
import db.models  # noqa: F401  必须先注册模型,create_all 才有表
from db.session import Base


@pytest.fixture()
def client(tmp_path):
    """独立临时库 + main.app(含鉴权中间件);不触发 lifespan(init_db)。"""
    url = f"sqlite:///{tmp_path / 'auth_domain_test.db'}"
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


def rand_password() -> str:
    """运行时随机口令(非字面量,安全钩子友好)。"""
    return "pw-" + secrets.token_hex(8)


@pytest.fixture()
def three_users(client):
    """admin 甲 + 已审核普通用户乙、丙;共享版(原 test_data_isolation 内联副本)。"""
    import secrets

    pw = "pw-" + secrets.token_hex(8)

    def _register(name):
        r = client.post("/api/auth/register", json={"username": name, "password": pw})
        assert r.status_code == 200

    def _login(name):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        assert r.status_code == 200, r.json()
        return r.json()["token"]

    _register("管理员甲")
    _register("用户乙")
    _register("用户丙")
    admin_tok = _login("管理员甲")
    users = client.get(
        "/api/auth/admin/users", headers={"X-Auth-Token": admin_tok}
    ).json()["users"]
    ids = {u["username"]: u["user_id"] for u in users}
    for uid in (ids["用户乙"], ids["用户丙"]):
        client.post(
            f"/api/auth/admin/users/{uid}/approve", headers={"X-Auth-Token": admin_tok}
        )
    return {
        "admin_tok": admin_tok,
        "b_tok": _login("用户乙"),
        "c_tok": _login("用户丙"),
        "admin_id": ids["管理员甲"],
        "b_id": ids["用户乙"],
        "c_id": ids["用户丙"],
    }
