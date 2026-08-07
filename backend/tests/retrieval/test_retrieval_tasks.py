"""英文检索后台任务 API 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.models import PaperModel, RetrievalTaskModel
from src.db.session import SessionLocal, init_db
from src.retrieval.types import Paper, Source


client = TestClient(app)


def _clear_tasks():
    init_db()
    with SessionLocal() as db:
        db.query(RetrievalTaskModel).delete()
        db.query(PaperModel).delete()
        db.commit()


def _paper() -> Paper:
    return Paper(
        lit_id="lit_" + "a" * 16,
        source=Source.OPENALEX,
        title="Seafood marketing strategy",
        authors=["Alice"],
        journal="Aquaculture Reports",
        year=2024,
        abstract="marketing strategy for seafood",
        doi="10.1/demo",
        source_url="https://example.com/demo",
        cited_by_count=12,
    )


class TestRetrievalTasks:
    def setup_method(self):
        _clear_tasks()

    def test_create_task_persists_and_finishes_after_request_disconnect(self, monkeypatch):
        monkeypatch.setattr(
            "retrieval.task_service.plan_query",
            lambda topic, default_year_start: {
                "topic_summary": topic,
                "keywords_en": ["seafood", "marketing"],
            },
        )
        monkeypatch.setattr(
            "retrieval.task_service.OpenAlexAdapter.search",
            lambda self, query, year_range, per_page: [_paper()],
        )
        monkeypatch.setattr(
            "retrieval.task_service.rerank",
            lambda papers, topic, top_n: papers,
        )
        monkeypatch.setattr(
            "api.retrieval_tasks.create_task",
            lambda **kwargs: __import__("retrieval.task_service", fromlist=["create_task"]).create_task(
                **kwargs,
                run_inline=True,
            ),
        )

        resp = client.post(
            "/api/retrieval/tasks",
            json={
                "topic": "水产品营销策略",
                "year_start": 2020,
                "year_end": 2026,
                "min_citations": 0,
                "limit": 20,
                "use_rerank": True,
            },
        )
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        detail = client.get(f"/api/retrieval/tasks/{task_id}")
        assert detail.status_code == 200
        data = detail.json()
        assert data["task_id"] == task_id
        assert data["status"] == "succeeded"
        assert data["progress"] == 100
        assert data["query_used"] == "seafood marketing"
        assert len(data["papers"]) == 1
        assert data["papers"][0]["lit_id"] == "lit_" + "a" * 16

    def test_list_tasks_returns_history(self, monkeypatch):
        monkeypatch.setattr(
            "src.retrieval.task_service.plan_query",
            lambda topic, default_year_start: {
                "topic_summary": topic,
                "keywords_en": ["seafood"],
            },
        )
        monkeypatch.setattr(
            "retrieval.task_service.OpenAlexAdapter.search",
            lambda self, query, year_range, per_page: [],
        )

        client.post(
            "/api/retrieval/tasks",
            json={"topic": "水产品营销策略", "limit": 5},
        )
        resp = client.get("/api/retrieval/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) >= 1
        assert tasks[0]["topic"] == "水产品营销策略"
        assert tasks[0]["status"] in {"pending", "running", "succeeded", "failed"}

    def test_succeeded_task_upserts_into_paper_pool(self, monkeypatch):
        """检索成功后,结果应自动写入文献池,即使无用户手动操作也可从 /papers 读出。"""
        monkeypatch.setattr(
            "retrieval.task_service.plan_query",
            lambda topic, default_year_start: {
                "topic_summary": topic,
                "keywords_en": ["seafood"],
            },
        )
        monkeypatch.setattr(
            "retrieval.task_service.OpenAlexAdapter.search",
            lambda self, query, year_range, per_page: [_paper()],
        )
        monkeypatch.setattr(
            "retrieval.task_service.rerank",
            lambda papers, topic, top_n: papers,
        )
        monkeypatch.setattr(
            "api.retrieval_tasks.create_task",
            lambda **kwargs: __import__(
                "retrieval.task_service", fromlist=["create_task"]
            ).create_task(**kwargs, run_inline=True),
        )

        resp = client.post(
            "/api/retrieval/tasks",
            json={
                "topic": "水产品营销策略",
                "year_start": 2020,
                "year_end": 2026,
                "min_citations": 0,
                "limit": 20,
                "use_rerank": True,
            },
        )
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        # 等待任务进入 succeeded
        for _ in range(40):
            detail = client.get(f"/api/retrieval/tasks/{task_id}").json()
            if detail["status"] == "succeeded":
                break
            import time as _t
            _t.sleep(0.1)
        else:
            pytest.fail(f"任务未在超时内完成:{detail['status']}")

        # 关键断言:文献池已自动入库
        pool = client.get("/api/papers").json()
        assert any(p["lit_id"] == "lit_" + "a" * 16 for p in pool), (
            "检索成功后应当自动写入文献池,以保证切换页面后数据不丢失"
        )

    def test_delete_task_removes_papers(self, monkeypatch):
        """删除任务时,应同步清理由该任务入库到文献池的英文文献。"""
        monkeypatch.setattr(
            "retrieval.task_service.plan_query",
            lambda topic, default_year_start: {
                "topic_summary": topic,
                "keywords_en": ["seafood"],
            },
        )
        monkeypatch.setattr(
            "retrieval.task_service.OpenAlexAdapter.search",
            lambda self, query, year_range, per_page: [_paper()],
        )
        monkeypatch.setattr(
            "retrieval.task_service.rerank",
            lambda papers, topic, top_n: papers,
        )
        monkeypatch.setattr(
            "api.retrieval_tasks.create_task",
            lambda **kwargs: __import__(
                "retrieval.task_service", fromlist=["create_task"]
            ).create_task(**kwargs, run_inline=True),
        )

        # 跑一个检索任务并确认有 1 篇入库
        r = client.post(
            "/api/retrieval/tasks",
            json={"topic": "删除测试", "limit": 10},
        )
        task_id = r.json()["task_id"]
        import time as _t
        for _ in range(40):
            d = client.get(f"/api/retrieval/tasks/{task_id}").json()
            if d["status"] == "succeeded":
                break
            _t.sleep(0.1)
        else:
            pytest.fail(f"任务未完成:{d['status']}")

        pool_before = client.get("/api/papers").json()
        assert any(p["lit_id"] == "lit_" + "a" * 16 for p in pool_before)

        # 删除任务
        resp = client.delete(f"/api/retrieval/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_deleted"] is True
        assert body["papers_deleted"] >= 1

        # 任务与对应入库文献都不应存在
        assert client.get(f"/api/retrieval/tasks/{task_id}").status_code == 404
        pool_after = client.get("/api/papers").json()
        assert not any(p["lit_id"] == "lit_" + "a" * 16 for p in pool_after)

        # 删除不存在的任务应该返回 task_deleted=False
        bogus = client.delete(f"/api/retrieval/tasks/{task_id}").json()
        assert bogus["task_deleted"] is False
