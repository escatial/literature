# -*- coding: utf-8 -*-
"""文献池/检索侧停止隔离回归(D2,2026-10-27):四象限 + `__default__` 兜底键。

与 tests/writing/test_stop_isolation.py(C2 写作流,注册表键隔离)不对称:
检索侧(B6)以 DB 行 owner_user_id 为权威 —— 服务层 stop_tasks 直测四象限,
文献池 `__default__` 兜底桶(缺 X-Task-Id 时 _resolve_task_id 回退)验证
「兜底键不破坏 owner 隔离」。

凭据/ID 全部运行时随机生成。
"""
import secrets
import threading
from datetime import datetime, timezone

import fastapi
import pytest
from fastapi.testclient import TestClient

import db.session as db_session
from db.models import PaperModel, RetrievalTaskModel, UserModel
from db.session import Base
from retrieval.task_service import _CANCEL_EVENTS, _CANCEL_REQUESTED, stop_tasks


@pytest.fixture()
def db_env(tmp_path):
    """独立临时库 + 干净的取消注册表。"""
    db_session.reset_engine(f"sqlite:///{tmp_path / f'pool_stop_{secrets.token_hex(4)}.db'}")
    Base.metadata.create_all(bind=db_session.engine)
    _CANCEL_EVENTS.clear()
    _CANCEL_REQUESTED.clear()
    yield db_session
    _CANCEL_EVENTS.clear()
    _CANCEL_REQUESTED.clear()
    db_session.engine.dispose()


@pytest.fixture()
def client(db_env):
    """只挂 papers 路由的轻量 FastAPI 实例(与 tests/pool/test_task_topic 同构)。"""
    from api.papers import router

    app = fastapi.FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_task(db, task_id: str, owner: str) -> None:
    with db.SessionLocal() as session:
        session.add(RetrievalTaskModel(
            task_id=task_id, topic=f"任务-{task_id[:8]}", status="running",
            owner_user_id=owner, updated_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _register_cancel(task_id: str) -> threading.Event:
    """模拟任务线程已注册取消事件(stop_tasks 可置位)。"""
    ev = threading.Event()
    _CANCEL_EVENTS[task_id] = ev
    return ev


def _paper_payload(title: str) -> dict:
    """构造可通过身份指纹校验的 paper(lit_id 由 build_lit_id 派生)。"""
    from retrieval.paper_identity import build_lit_id

    base = {
        "source": "user_imported", "title": title,
        "authors": ["李四"], "journal": "测试学报", "year": 2024,
        "abstract": "测试摘要", "selected": True,
    }
    return {**base, "lit_id": build_lit_id(
        source=base["source"], title=base["title"],
        authors=base["authors"], year=base["year"],
    )}


# ---------- 象限1:A 停 A 生效 ----------

def test_owner_stops_own_tasks_both_modes(db_env):
    """A 显式停自己的任务生效;A 全停(不带 task_ids)收窄为名下全部。"""
    uid_a = f"usr_{secrets.token_hex(4)}"
    task_a1 = f"task-a1-{secrets.token_hex(5)}"
    task_a2 = f"task-a2-{secrets.token_hex(5)}"
    _seed_task(db_env, task_a1, uid_a)
    _seed_task(db_env, task_a2, uid_a)
    ev_a1 = _register_cancel(task_a1)
    ev_a2 = _register_cancel(task_a2)

    # 显式指定自己的任务
    assert stop_tasks([task_a1], user_id=uid_a, is_admin=False) == [task_a1]
    assert ev_a1.is_set() and not ev_a2.is_set()

    # 全停:收窄为名下全部(task_a2 被置位)
    ev_a1.clear()
    assert sorted(stop_tasks(None, user_id=uid_a, is_admin=False)) == sorted([task_a1, task_a2])
    assert ev_a1.is_set() and ev_a2.is_set()


# ---------- 象限2:A 停 B 无效 ----------

def test_owner_cannot_stop_other_users_tasks(db_env):
    """A 显式停 B 的任务:越界剔除(stopped=[]),事件未置位,
    v9.6 停止账本也不记账(防注册窗口补置位)。"""
    uid_a = f"usr_{secrets.token_hex(4)}"
    uid_b = f"usr_{secrets.token_hex(4)}"
    task_b = f"task-b-{secrets.token_hex(5)}"
    _seed_task(db_env, task_b, uid_b)
    ev_b = _register_cancel(task_b)

    assert stop_tasks([task_b], user_id=uid_a, is_admin=False) == []
    assert not ev_b.is_set()
    assert task_b not in _CANCEL_REQUESTED

    # A 全停:名下无任务 → 返回 [](不误伤 B)
    assert stop_tasks(None, user_id=uid_a, is_admin=False) == []
    assert not ev_b.is_set()


# ---------- 象限3:admin 停任意生效 ----------

def test_admin_stops_any_users_task(db_env):
    """admin 显式停任意用户任务:不做 owner 过滤,直接置位。"""
    admin = f"usr_{secrets.token_hex(4)}"
    uid_b = f"usr_{secrets.token_hex(4)}"
    task_b = f"task-b-{secrets.token_hex(5)}"
    _seed_task(db_env, task_b, uid_b)
    ev_b = _register_cancel(task_b)

    assert stop_tasks([task_b], user_id=admin, is_admin=True) == [task_b]
    assert ev_b.is_set()

    # admin 全停:B 的任务一并命中
    ev_b.clear()
    assert task_b in stop_tasks(None, user_id=admin, is_admin=True)
    assert ev_b.is_set()


# ---------- 象限4:`__default__` 兜底键 ----------

def test_resolve_task_id_fallback_to_default():
    """缺 X-Task-Id / 空白值回退 __default__;非空值 strip 后原样返回。"""
    from api.papers import _DEFAULT_TASK_ID, _resolve_task_id

    assert _resolve_task_id(None) == _DEFAULT_TASK_ID
    assert _resolve_task_id("") == _DEFAULT_TASK_ID
    assert _resolve_task_id("   ") == _DEFAULT_TASK_ID
    raw = f"t-{secrets.token_hex(4)}"
    assert _resolve_task_id(f"  {raw}  ") == raw


def test_default_bucket_via_http_no_header(db_env, client):
    """无 X-Task-Id 的 bulk 写入全部落 __default__ 桶;无 header 的读取只见
    该桶;显式其他 task 的读取不见(兜底键是业务域隔离的一员)。"""
    r = client.post("/papers/bulk", json={"papers": [
        _paper_payload("兜底桶文献一"), _paper_payload("兜底桶文献二"),
    ]})
    assert r.status_code == 200 and r.json()["inserted"] == 2

    with db_env.SessionLocal() as session:
        rows = session.query(PaperModel).all()
    assert {row.task_id for row in rows} == {"__default__"}

    # 无 header 读取 → __default__ 桶可见
    r = client.get("/papers")
    assert r.json()["total"] == 2
    # 显式其他 task 读取 → 不可见(兜底桶不跨业务域泄漏)
    r = client.get("/papers", headers={"X-Task-Id": f"t-{secrets.token_hex(6)}"})
    assert r.json()["total"] == 0


def test_default_bucket_respects_owner_isolation(db_env, client):
    """兜底桶上仍按 owner 收口:A、B 各自无 header 写入共落 __default__,
    普通用户只见自己名下;admin 直通全见(双层正交回归)。"""
    uid_a = f"usr_{secrets.token_hex(4)}"
    uid_b = f"usr_{secrets.token_hex(4)}"
    with db_env.SessionLocal() as session:
        session.add(UserModel(
            user_id=uid_a, username=f"甲{secrets.token_hex(3)}",
            email=f"a{secrets.token_hex(3)}@local.dev", password_hash="x",
            role="user", status="approved", enabled=True,
        ))
        session.add(UserModel(
            user_id=uid_b, username=f"乙{secrets.token_hex(3)}",
            email=f"b{secrets.token_hex(3)}@local.dev", password_hash="x",
            role="user", status="approved", enabled=True,
        ))
        session.commit()
    with db_env.SessionLocal() as session:
        session.add(PaperModel(
            lit_id=f"lit_{secrets.token_hex(8)}", source="user_imported",
            title="甲的兜底文献", authors=["甲"], journal="学报", year=2024,
            abstract="摘要", selected=True, task_id="__default__",
            owner_user_id=uid_a,
        ))
        session.add(PaperModel(
            lit_id=f"lit_{secrets.token_hex(8)}", source="user_imported",
            title="乙的兜底文献", authors=["乙"], journal="学报", year=2024,
            abstract="摘要", selected=True, task_id="__default__",
            owner_user_id=uid_b,
        ))
        session.commit()

    from api.papers import apply_owner_scope

    with db_env.SessionLocal() as session:
        class _U:
            def __init__(self, uid: str, role: str = "user"):
                self.user_id = uid
                self.role = role

        as_a = apply_owner_scope(session.query(PaperModel), _U(uid_a)).all()
        as_admin = apply_owner_scope(session.query(PaperModel), _U("x", role="admin")).all()

    assert [r.owner_user_id for r in as_a] == [uid_a]
    assert len(as_admin) == 2


def test_stop_does_not_touch_default_pool_bucket(db_env, client):
    """停止授权与兜底池互不串扰:对不存在任务的停止请求不产生副作用,
    __default__ 桶文献原样保留。"""
    r = client.post("/papers/bulk", json={"papers": [_paper_payload("停止前的兜底文献")]})
    assert r.status_code == 200

    uid_a = f"usr_{secrets.token_hex(4)}"
    assert stop_tasks([f"task-none-{secrets.token_hex(5)}"], user_id=uid_a, is_admin=False) == []

    with db_env.SessionLocal() as session:
        assert session.query(PaperModel).count() == 1
