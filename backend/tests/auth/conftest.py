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
