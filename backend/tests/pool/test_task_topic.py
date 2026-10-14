# -*- coding: utf-8 -*-
"""GET /papers/task-topic 契约测试:写作页自动填充研究主题的数据来源。

- 有检索历史的任务 → 返回该任务最新一条历史的主题;
- 无历史 / 缺 X-Task-Id → 返回空串,前端保持输入框原状;
- /task-topic 必须先于 /{lit_id} 注册,否则路径参数会吞掉它。
"""
import fastapi
import pytest
from fastapi.testclient import TestClient

import db.session as db_session
from db.models import PaperModel, RetrievalHistoryModel
from db.session import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立内存库 + 只挂 papers 路由的 FastAPI 实例。"""
    url = f"sqlite:///{tmp_path / 'pool_test.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    from api.papers import router

    app = fastapi.FastAPI()
    app.include_router(router)
    yield TestClient(app)
    db_session.engine.dispose()


def _seed_history(task_id: str, topic: str, **kw) -> None:
    from datetime import datetime, timezone

    row = RetrievalHistoryModel(
        task_id=task_id,
        topic=topic,
        sources=kw.get("sources", ["cnki", "openalex", "pubmed"]),
        created_at=kw.get("created_at", datetime.now(timezone.utc)),
    )
    with db_session.SessionLocal() as db:
        db.add(row)
        db.commit()


def test_returns_topic_of_current_task(client):
    _seed_history("t-20261013-aaaa", "水产品营销策略研究")
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-20261013-aaaa"})
    assert resp.status_code == 200
    assert resp.json() == {"topic": "水产品营销策略研究"}


def test_returns_latest_topic_when_multiple(client):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    _seed_history("t-20261013-bbbb", "旧主题", created_at=now - timedelta(hours=2))
    _seed_history("t-20261013-bbbb", "新主题", created_at=now)
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-20261013-bbbb"})
    assert resp.json() == {"topic": "新主题"}


def test_isolated_from_other_tasks(client):
    _seed_history("t-20261013-cccc", "别的任务的主题")
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-20261013-dddd"})
    assert resp.status_code == 200
    assert resp.json() == {"topic": ""}


def test_empty_topic_and_missing_header_return_empty(client):
    # 有历史但主题为空串
    _seed_history("t-20261013-eeee", "")
    r1 = client.get("/papers/task-topic", headers={"X-Task-Id": "t-20261013-eeee"})
    # 完全没有历史(缺 header 回退 __default__)
    r2 = client.get("/papers/task-topic")
    assert r1.json() == {"topic": ""}
    assert r2.status_code == 200
    assert r2.json() == {"topic": ""}


def test_task_topic_not_swallowed_by_lit_id_route(client):
    """/task-topic 注册在 /{lit_id} 之前:不能被单篇查询路由当成 lit_id 吞掉。"""
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-nobody"})
    assert resp.status_code == 200
    assert "topic" in resp.json()


def test_resolves_topic_by_snapshot_overlap(client):
    """「查看历史」恢复进新任务池:history.task_id 还是原始任务,
    应按 lit_id 快照重合度(过半)回退解析出来源主题。"""
    snap = [{"lit_id": f"lit{i:03d}", "title": f"文献{i}"} for i in range(10)]
    _seed_history("t-original-task", "水产品营销策略")
    with db_session.SessionLocal() as db:
        row = db.query(RetrievalHistoryModel).filter(
            RetrievalHistoryModel.task_id == "t-original-task"
        ).first()
        row.papers_snapshot = snap
        for s in snap:
            db.add(PaperModel(
                lit_id=s["lit_id"],
                source="cnki",
                title=s["title"],
                year=2024,
                abstract="摘要",
                abstract_text="摘要",
                task_id="t-restored-task",
            ))
        db.commit()
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-restored-task"})
    assert resp.json() == {"topic": "水产品营销策略"}


def test_overlap_below_half_returns_empty(client):
    """池内文献与任何历史快照重合不过半(如手动导入)→ 不乱认主题。"""
    snap = [{"lit_id": f"lit{i:03d}", "title": f"文献{i}"} for i in range(4)]
    _seed_history("t-original-task", "某主题")
    with db_session.SessionLocal() as db:
        row = db.query(RetrievalHistoryModel).filter(
            RetrievalHistoryModel.task_id == "t-original-task"
        ).first()
        row.papers_snapshot = snap
        for i in range(10):
            db.add(PaperModel(
                lit_id=f"manual{i:03d}",
                source="user_imported",
                title=f"手动文献{i}",
                year=2024,
                abstract="摘要",
                abstract_text="摘要",
                task_id="t-manual-task",
            ))
        db.commit()
    resp = client.get("/papers/task-topic", headers={"X-Task-Id": "t-manual-task"})
    assert resp.json() == {"topic": ""}


def test_pool_still_isolated_by_task(client):
    """顺带回归:同库内其他任务的 papers 不会串池(与主题解析共用 task 语义)。"""
    with db_session.SessionLocal() as db:
        db.add(PaperModel(
            lit_id="a" * 32,
            source="cnki",
            title="甲任务文献",
            year=2024,
            abstract="摘要",
            abstract_text="摘要",
            task_id="t-task-a",
        ))
        db.commit()
    resp = client.get("/papers", headers={"X-Task-Id": "t-task-b"})
    assert resp.json()["total"] == 0
    resp = client.get("/papers", headers={"X-Task-Id": "t-task-a"})
    assert resp.json()["total"] == 1
