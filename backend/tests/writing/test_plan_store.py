# -*- coding: utf-8 -*-
"""规划快照存取回归(C5)。

- upsert/get/latest 基本链路 + payload 完整性;
- 重复规划同 task_id 覆盖不重复插;
- 跨用户取不到(非 admin 404 语义,防存在性泄漏);
- latest 只返回本人名下最近一条。
"""
import secrets

import pytest

import db.session as db_session
from db.models import UserModel
from db.session import Base
from writing.plan_store import (
    get_plan,
    latest_plan,
    plan_to_writing_payload,
    upsert_plan,
)


@pytest.fixture()
def plan_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'plan_store.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    yield
    db_session.engine.dispose()


def _user(role: str = "user") -> UserModel:
    return UserModel(
        user_id=f"usr_{secrets.token_hex(6)}",
        username=f"{role}-{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@local.dev",
        password_hash="x", role=role, status="approved", enabled=True,
    )


def _payload(topic: str = "测试主题") -> dict:
    return {
        "topic": topic,
        "classify_mode": "theme",
        "groups": [{"name": "一、主题A", "lit_ids": ["lit_aaa", "lit_bbb"]}],
        "section_titles": ["一、主题A"],
        "screened_lit_ids": ["lit_aaa", "lit_bbb"],
        "relevance_report": {"summary": {"high": 2}},
        "blueprint": {"research_question": topic},
        "research_questions": ["RQ1"],
        "review_goal": "目标",
    }


def test_upsert_get_roundtrip_and_payload_shape(plan_db):
    admin = _user("admin")
    task_id = f"task-{secrets.token_hex(5)}"
    row = upsert_plan(task_id, admin.user_id, _payload())
    got = get_plan(task_id, admin)
    assert got is not None and got.task_id == task_id
    out = plan_to_writing_payload(got)
    assert out["screened_lit_ids"] == ["lit_aaa", "lit_bbb"]
    assert out["groups"][0]["name"] == "一、主题A"
    assert out["research_questions"] == ["RQ1"]
    assert out["relevance_report"]["summary"]["high"] == 2


def test_duplicate_plan_upsert_overwrites_not_inserts(plan_db):
    u = _user("user")
    task_id = f"task-{secrets.token_hex(5)}"
    upsert_plan(task_id, u.user_id, _payload("第一版"))
    upsert_plan(task_id, u.user_id, _payload("第二版覆盖"))
    got = get_plan(task_id, u)
    assert got.payload["topic"] == "第二版覆盖"
    with db_session.SessionLocal() as session:
        count = session.query(type(got)).filter_by(task_id=task_id).count()
    assert count == 1  # 不重复插


def test_cross_user_access_denied_admin_bypasses(plan_db):
    owner = _user("user")
    other = _user("user")
    admin = _user("admin")
    task_id = f"task-{secrets.token_hex(5)}"
    upsert_plan(task_id, owner.user_id, _payload())
    # 非本人:取不到(404 语义)
    assert get_plan(task_id, other) is None
    # admin 直通
    assert get_plan(task_id, admin) is not None
    # 本人可取
    assert get_plan(task_id, owner) is not None


def test_latest_scoped_to_owner(plan_db):
    a, b = _user("user"), _user("user")
    upsert_plan(f"task-{secrets.token_hex(5)}", a.user_id, _payload("甲的规划"))
    upsert_plan(f"task-{secrets.token_hex(5)}", b.user_id, _payload("乙的规划"))
    assert latest_plan(a).payload["topic"] == "甲的规划"
    assert latest_plan(b).payload["topic"] == "乙的规划"
    # user=None(测试/内部直调上下文)与全库 owner 约定一致:直通全库最近一条
    # (生产由鉴权中间件保证 user 非空,与 api/papers.apply_owner_scope 同语义)
    assert latest_plan(None) is not None
