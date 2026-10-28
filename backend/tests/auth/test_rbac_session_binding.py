# -*- coding: utf-8 -*-
"""rbac 会话绑定回归(2026-10-27 SessionLocal 属性访问修复)。

历史 bug:rbac.py `from db.session import SessionLocal` 在导入期绑定了
当时的 sessionmaker 对象;db.session.reset_engine(测试隔离)重建全局后,
该绑定仍指向旧 engine —— create_user/authenticate 读写旧库("写穿")。
security/auth.py 已修复为 `from db import session as db_session` +
`db_session.SessionLocal` 属性访问(见其模块头注释),本测试锁定 rbac
同语义:reset_engine 之后 rbac 的写入与查询必须落新库。

凭据全部运行时随机生成,源码无字面量口令。
"""
import secrets
import uuid

import pytest

import db.session as db_session
import db.models  # noqa: F401  必须先注册模型,create_all 才有表
from db.models import UserModel
from db.session import Base
from security import rbac


@pytest.fixture()
def fresh_engine(tmp_path):
    """独立临时库 A(reset_engine),不碰真实 lit_review.db。"""
    url = f"sqlite:///{tmp_path / 'rbac_binding.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    yield
    db_session.engine.dispose()


def test_rbac_writes_follow_reset_engine(fresh_engine, tmp_path):
    """reset_engine 切库后,rbac 的读写必须跟随新全局绑定。"""
    email_a = f"rbac-{uuid.uuid4().hex[:8]}@test.local"
    pw_a = "pw-" + secrets.token_hex(8)
    row = rbac.create_user(
        username=f"rb_{uuid.uuid4().hex[:6]}",
        email=email_a,
        password=pw_a,
    )
    assert row is not None and row.user_id

    # 切到全新库 B:全局 SessionLocal 已被 reset_engine 重绑
    url_b = f"sqlite:///{tmp_path / 'rbac_binding_b.db'}"
    db_session.reset_engine(url_b)
    Base.metadata.create_all(bind=db_session.engine)

    # 前置:新库 B 里没有该用户(新全局绑定视角)
    with db_session.SessionLocal() as db:
        assert db.query(UserModel).filter_by(email=email_a).one_or_none() is None

    # 核心:rbac.create_user 是 upsert-by-email —— 修复前它经旧绑定读到
    # 库 A 里的既有行直接返回(写穿旧库),B 永远建不出用户;修复后
    # 属性访问命中新绑定,B 中应出现该用户。
    pw_b = "pw-" + secrets.token_hex(8)
    created = rbac.create_user(
        username=f"rb_{uuid.uuid4().hex[:6]}",
        email=email_a,
        password=pw_b,
    )
    with db_session.SessionLocal() as db:
        found = db.query(UserModel).filter_by(email=email_a).one_or_none()
    assert found is not None, "rbac 写入未跟随 reset_engine:仍停留在旧 engine(写穿)"
    assert found.user_id == created.user_id
    # 同一绑定链路上的 authenticate 也应命中新库并校验通过
    assert rbac.authenticate(email=email_a, password=pw_b) is not None
    assert rbac.authenticate(email=email_a, password=pw_a) is None
