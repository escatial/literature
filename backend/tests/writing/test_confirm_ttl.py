# -*- coding: utf-8 -*-
"""确认窗口生命周期测试(TTL 过期 + 新任务取代)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from writing import job_store  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """独立临时库:本测试依赖固定 job_id 的一次性插入,不能依赖共享 DB 的
    残留状态(此前无 fixture:全量顺序下靠前序测试建表,孤立/二次运行必撞
    UNIQUE——2026-11-18 基线实测暴露的顺序耦合)。"""
    from db.session import Base, reset_engine

    reset_engine(f"sqlite:///{tmp_path / 'ttl_test.db'}")
    import db.models  # noqa: F401
    Base.metadata.create_all(bind=reset_engine(f"sqlite:///{tmp_path / 'ttl_test.db'}"))
    yield
    reset_engine("sqlite:///:memory:")


def test_expire_stale_awaiting(monkeypatch, fresh_db):
    """超过 TTL 的 awaiting_confirm → failed + 快照清空;新鲜的保留。"""
    from db.models import _utcnow, WritingJobModel
    from db.session import SessionLocal
    from datetime import timedelta

    monkeypatch.setenv("WRITING_CONFIRM_TTL_MINUTES", "120")
    job_store.create_job("job-ttl-old", "usr_t", "主题", "theme", {"k": "v"})
    job_store.create_job("job-ttl-new", "usr_t", "主题", "theme", {"k": "v"})
    job_store.set_job_status("job-ttl-old", "awaiting_confirm")
    job_store.set_job_status("job-ttl-new", "awaiting_confirm")
    # 把旧任务的 updated_at 拨回 3 小时前
    with SessionLocal() as db:
        row = db.query(WritingJobModel).filter_by(job_id="job-ttl-old").first()
        row.updated_at = _utcnow() - timedelta(hours=3)
        db.commit()

    n = job_store.expire_stale_awaiting()
    assert n >= 1
    old = job_store.get_job("job-ttl-old")
    assert old["status"] == "failed"
    # P0-5:错误码归一后「超时过期」仍在文案中,且带错误码前缀
    assert "超时过期" in (old["error"] or "")
    assert "[错误码 confirm_expired]" in (old["error"] or "")
    assert old["req_snapshot"] == {}  # 快照清空:恢复机制不再端出旧方案
    new = job_store.get_job("job-ttl-new")
    assert new["status"] == "awaiting_confirm"  # 新鲜任务不受影响

    job_store.stop_job("job-ttl-new")


def test_supersede_awaiting_except(monkeypatch, fresh_db):
    """新任务创建后,同属主旧 awaiting_confirm 被取代;他属主不受影响。"""
    job_store.create_job("job-sup-old", "usr_s", "主题", "theme", None)
    job_store.create_job("job-sup-other", "usr_other", "主题", "theme", None)
    job_store.set_job_status("job-sup-old", "awaiting_confirm")
    job_store.set_job_status("job-sup-other", "awaiting_confirm")

    n = job_store.supersede_awaiting_except("usr_s", keep_job_id="job-sup-new")
    assert n >= 1
    assert job_store.get_job("job-sup-old")["status"] == "stopped"
    # 他属主的 awaiting_confirm 不被动
    assert job_store.get_job("job-sup-other")["status"] == "awaiting_confirm"

    job_store.stop_job("job-sup-other")
