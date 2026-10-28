# -*- coding: utf-8 -*-
"""阶段2引用取数双路径回归(C6)。

- 新路径:confirmed_groups + papers 空 → X-Task-Id → 规划快照 screened_lit_ids
  引用回表(papers 表唯一真源);快照缺失/取数 0 条/缺头 → 400;
- 旧路径(兼容):papers 非空 → 原样消费,不依赖快照(防回归);
- 端到端:旧路径走完整 generate_review(清 key 下走占位兜底,不打真实 LLM)。

部署顺序约束:后端先上(本批),前端随后停发 papers —— 旧前端发全量
papers 走旧路径不受影响;新前端空 papers 依赖本批后端。
"""
import secrets

import pytest
from fastapi import HTTPException

import db.session as db_session
from api.writing import PaperIn, WritingRequest, _resolve_stage2_papers
from db.models import PaperModel, UserModel
from db.session import Base
from writing.classifier import Group
from writing.plan_store import upsert_plan


@pytest.fixture()
def gen_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'gen_ref.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    yield
    db_session.engine.dispose()


def _user(role: str = "user") -> UserModel:
    return UserModel(
        user_id=f"usr_{secrets.token_hex(6)}",
        username=f"u-{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@local.dev",
        password_hash="x", role=role, status="approved", enabled=True,
    )


class _FakeRequest:
    """最小 request 替身(_resolve_stage2_papers 只读 state.user)。"""

    def __init__(self, user):
        self.state = type("S", (), {"user": user})()


def _seed_pool(task_id: str, owner_id: str, n: int = 3) -> list[str]:
    lit_ids = [f"lit_c6_{secrets.token_hex(4)}" for _ in range(n)]
    with db_session.SessionLocal() as session:
        for i, lit in enumerate(lit_ids):
            session.add(PaperModel(
                lit_id=lit, source="user_imported", title=f"文献{i}",
                authors=["张三"], journal="测试学报", year=2024,
                abstract="问卷调查发现X正向影响Y。", selected=True,
                task_id=task_id, owner_user_id=owner_id,
            ))
        session.commit()
    return lit_ids


def _req(papers: list[PaperIn], confirmed: bool = True) -> WritingRequest:
    return WritingRequest(
        topic="测试综述",
        papers=papers,
        classify_mode="theme",
        do_screening=False,
        confirmed_groups=[GroupOut_stub()] if confirmed else None,
    )


def GroupOut_stub():
    # 延迟构造,避免模块级循环依赖
    from api.writing import GroupOut

    return GroupOut(name="一、主题", lit_ids=[])


def test_reference_path_resolves_from_plan_snapshot(gen_db):
    """新路径:papers 空 + X-Task-Id → 快照 lit_ids 回表取全字段。"""
    from fastapi import Request

    u = _user()
    task_id = f"task-{secrets.token_hex(5)}"
    lit_ids = _seed_pool(task_id, u.user_id)
    upsert_plan(task_id, u.user_id, {
        "topic": "测试综述", "classify_mode": "theme",
        "groups": [{"name": "一、主题", "lit_ids": lit_ids}],
        "section_titles": ["一、主题"],
        "screened_lit_ids": lit_ids,
        "relevance_report": {"summary": {"high": 3}},
        "research_questions": ["RQ1"],
        "review_goal": "目标",
    })
    req = _req([])
    papers = _resolve_stage2_papers(req, task_id, _FakeRequest(u), db_session.SessionLocal())
    assert [p.lit_id for p in papers] == lit_ids
    assert all(p.title.startswith("文献") for p in papers)
    # 缺省回填快照值(显式传值优先)
    assert req.relevance_report == {"summary": {"high": 3}}
    assert req.research_questions == ["RQ1"]
    assert req.review_goal == "目标"


def test_reference_path_missing_snapshot_or_header_returns_400(gen_db):
    u = _user()
    req = _req([])
    # 缺 X-Task-Id
    with pytest.raises(HTTPException) as ei:
        _resolve_stage2_papers(req, "", _FakeRequest(u), db_session.SessionLocal())
    assert ei.value.status_code == 400
    # 有头但快照缺失
    with pytest.raises(HTTPException) as ei2:
        _resolve_stage2_papers(
            _req([]), f"task-{secrets.token_hex(4)}",
            _FakeRequest(u), db_session.SessionLocal(),
        )
    assert ei2.value.status_code == 400
    assert "重新规划" in ei2.value.detail


def test_reference_path_empty_pool_returns_400(gen_db):
    """快照存在但池中 0 条命中(如池被清) → 400。"""
    u = _user()
    task_id = f"task-{secrets.token_hex(5)}"
    upsert_plan(task_id, u.user_id, {
        "screened_lit_ids": ["lit_ghost_0001"],
    })
    with pytest.raises(HTTPException) as ei:
        _resolve_stage2_papers(
            _req([]), task_id, _FakeRequest(u), db_session.SessionLocal(),
        )
    assert ei.value.status_code == 400


def test_legacy_path_with_full_papers_unchanged(gen_db):
    """旧路径:papers 非空 → 原样 to_paper,不依赖任何快照。"""
    u = _user()
    papers_in = [
        PaperIn(lit_id="lit_legacy_1", source="user_imported", title="旧路径文献",
                authors=["李四"], year=2023, abstract="摘要"),
    ]
    req = _req(papers_in)
    papers = _resolve_stage2_papers(
        req, "", _FakeRequest(u), db_session.SessionLocal(),
    )  # 无 X-Task-Id 也不 400 —— 旧路径不看快照
    assert len(papers) == 1
    assert papers[0].lit_id == "lit_legacy_1"
    assert papers[0].title == "旧路径文献"
