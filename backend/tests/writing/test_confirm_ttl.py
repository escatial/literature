# -*- coding: utf-8 -*-
"""确认窗口生命周期测试(TTL 过期 + 新任务取代)。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from writing import job_store  # noqa: E402


def test_expire_stale_awaiting(monkeypatch):
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
    assert "超时过期" in (old["error"] or "")
    assert old["req_snapshot"] == {}  # 快照清空:恢复机制不再端出旧方案
    new = job_store.get_job("job-ttl-new")
    assert new["status"] == "awaiting_confirm"  # 新鲜任务不受影响

    job_store.stop_job("job-ttl-new")


def test_supersede_awaiting_except(monkeypatch):
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
