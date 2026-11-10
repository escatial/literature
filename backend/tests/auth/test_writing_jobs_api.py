# -*- coding: utf-8 -*-
"""J1 任务化改造 · /writing/jobs 端点回归测试。

覆盖:阶段1生命周期(awaiting_confirm)→ 确认 → 阶段2完成(done)+ 事件
重放(since_seq 续传)+ 停止语义(落库 + 协作式取消)+ 归属隔离。
LLM 全部 fake(monkeypatch api.writing 命名空间),不触网。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


def _fake_plan_stream(**kwargs):
    """阶段1 fake:两帧(start + plan_complete),papers 取实际入参的 lit_id
    (快照捕获 → confirm 回表按 screened_lit_ids 对齐 papers 池)。"""
    papers = [
        {"lit_id": p.lit_id, "title": p.title,
         "authors": list(p.authors or []), "year": p.year}
        for p in (kwargs.get("papers") or [])
    ]
    lit_ids = [p["lit_id"] for p in papers]

    def gen():
        yield _sse("start", {"topic": kwargs.get("topic"), "phase": "planning"})
        yield _sse("plan_complete", {
            "groups": [{"name": "品牌建设", "lit_ids": lit_ids}],
            "section_titles": ["一、品牌建设"],
            "papers": papers,
            "relevance_report": {"grades": {}},
        })
    return gen()


def _fake_generate_stream(**kwargs):
    """阶段2 fake:定稿 + complete(manifest);触发 persist 验证服务端落库接线。"""

    def gen():
        yield _sse("start", {"phase": "writing"})
        yield _sse("sections_finalized", {"sections": [
            {"key": "theme_1", "title": "一、品牌建设", "content": "正文引用[1]。",
             "citations": ["lit_x_1"], "density_warning": None},
        ]})
        yield _sse("reference_list", {"reference_list": "[1] 张三. 某文献[J]. 2020."})
        persist = kwargs.get("persist")
        if persist is not None:
            persist({
                "task_id": kwargs.get("task_id"),
                "topic": kwargs.get("topic"),
                "classify_mode": kwargs.get("classify_mode"),
                "sections": [{"key": "theme_1", "title": "一、品牌建设",
                              "content": "正文引用[1]。", "citations": ["lit_x_1"]}],
                "reference_list": "[1] 张三. 某文献[J]. 2020.",
                "screened_out_ids": [], "dropped_citations": [],
                "qa_report": {"summary": {"overall": "pass", "pass_rate": 1.0}},
                "abstract": "测试摘要。", "keywords": ["关键词"],
            })
        yield _sse("complete", {
            "phase": "complete",
            "manifest": {
                "sections": [{"key": "theme_1", "title": "一、品牌建设",
                              "chars": 7, "citations": 1}],
                "expected_sections": 1, "missing_sections": [],
                "reference_count": 1, "anchor_leaks": 0, "has_abstract": True,
            },
        })
    return gen()


@pytest.fixture()
def writing_env(monkeypatch):
    """fake 掉两个生成器入口(api.writing 命名空间),记录调用参数。"""
    import api.writing as aw

    calls = {"plan": [], "generate": []}

    def fake_plan(**kwargs):
        calls["plan"].append(kwargs)
        return _fake_plan_stream(**kwargs)

    def fake_generate(**kwargs):
        calls["generate"].append(kwargs)
        return _fake_generate_stream(**kwargs)

    monkeypatch.setattr(aw, "plan_review_stream", fake_plan)
    monkeypatch.setattr(aw, "generate_review_stream", fake_generate)
    return calls


def _paper_payload(title: str = "某文献") -> dict:
    """构造可通过身份指纹校验的池文献(lit_id 由 build_lit_id 派生)。"""
    from retrieval.paper_identity import build_lit_id

    base = {
        "source": "user_imported", "title": title,
        "authors": ["张三"], "journal": "测试学报", "year": 2024,
        "abstract": "测试摘要", "selected": True,
    }
    return {**base, "lit_id": build_lit_id(
        source=base["source"], title=base["title"],
        authors=base["authors"], year=base["year"],
    )}


def _seed_pool(client, tok: str, task_id: str) -> str:
    """把测试文献写入 papers 池(confirm 回表的唯一真源)。返回 lit_id。"""
    paper = _paper_payload()
    r = client.post(
        "/api/papers/bulk", json={"papers": [paper]},
        headers={"X-Auth-Token": tok, "X-Task-Id": task_id},
    )
    assert r.status_code == 200, r.text
    return paper["lit_id"]


def _headers(tok: str) -> dict:
    return {"X-Auth-Token": tok}


def _wait_status(client, tok: str, job_id: str, want: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(f"/api/writing/jobs/{job_id}", headers=_headers(tok))
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("status") == want:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {want} 超时,最后状态: {last}")


def _start_stage1(client, tok: str) -> str:
    r = client.post(
        "/api/writing/jobs", headers=_headers(tok),
        json={
            "topic": "测试主题", "classify_mode": "theme", "do_screening": True,
            "papers": [_paper_payload()],
        },
    )
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def test_job_stage1_to_confirm_to_done(client, three_users, writing_env):
    """全生命周期:启动 → awaiting_confirm → 确认 → done,事件连续落库。"""
    u = three_users
    job_id = _start_stage1(client, u["b_tok"])
    _seed_pool(client, u["b_tok"], job_id)

    st = _wait_status(client, u["b_tok"], job_id, "awaiting_confirm")
    assert st["phase"] == "await_confirm"

    r = client.post(
        f"/api/writing/jobs/{job_id}/confirm", headers=_headers(u["b_tok"]),
        json={"confirmed_groups": [{"name": "品牌建设", "lit_ids": [_seed_pool(client, u["b_tok"], job_id)]}]},
    )
    assert r.status_code == 202, r.text
    _wait_status(client, u["b_tok"], job_id, "done")

    # 阶段2接到了 fake 的 persist → 服务端权威落库(reviews 出现该 task_id 行)
    from db.models import ReviewModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        row = db.query(ReviewModel).filter_by(task_id=job_id).first()
        assert row is not None
        assert row.abstract == "测试摘要。"


def test_job_stream_replay_and_resume(client, three_users, writing_env):
    """事件重放:since_seq=0 取全量且 seq 连续;since_seq=k 只返回其后事件。"""
    u = three_users
    job_id = _start_stage1(client, u["b_tok"])
    _wait_status(client, u["b_tok"], job_id, "awaiting_confirm")

    r = client.get(f"/api/writing/jobs/{job_id}/stream", headers=_headers(u["b_tok"]))
    assert r.status_code == 200
    frames = [
        json.loads(line[len("data:"):].strip())
        for line in r.text.splitlines()
        if line.startswith("data: ")
    ]
    seqs = [f["seq"] for f in frames]
    assert seqs == list(range(1, len(seqs) + 1))
    assert frames[-1]["event"] == "plan_complete"

    # 续传:从第 1 帧之后接,只剩第 2 帧以后
    r2 = client.get(
        f"/api/writing/jobs/{job_id}/stream",
        headers=_headers(u["b_tok"]), params={"since_seq": 1},
    )
    frames2 = [
        json.loads(line[len("data:"):].strip())
        for line in r2.text.splitlines()
        if line.startswith("data: ")
    ]
    assert all(f["seq"] > 1 for f in frames2)
    assert frames2[-1]["event"] == "plan_complete"


def test_job_stop_running(client, three_users, writing_env, monkeypatch):
    """运行中停止:落库 stopped + 协作式取消生效。"""
    import api.writing as aw

    u = three_users

    def fake_generate(**kwargs):
        stop_event = kwargs.get("stop_event")

        def gen():
            yield _sse("start", {"phase": "writing"})
            # 阻塞等待停止信号(协作式取消)
            for _ in range(200):
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(0.05)
            yield _sse("stopped", {"message": "已停止写作任务"})
        return gen()

    monkeypatch.setattr(aw, "generate_review_stream", fake_generate)

    # 直接造一个 awaiting_confirm 任务再确认进入 running
    job_id = _start_stage1(client, u["b_tok"])
    _seed_pool(client, u["b_tok"], job_id)
    _wait_status(client, u["b_tok"], job_id, "awaiting_confirm")

    # 阶段2用 fake:先取消 plan fake(确认端点用 generate)
    lit = _seed_pool(client, u["b_tok"], job_id)
    r = client.post(
        f"/api/writing/jobs/{job_id}/confirm", headers=_headers(u["b_tok"]),
        json={"confirmed_groups": [{"name": "品牌建设", "lit_ids": [lit]}]},
    )
    assert r.status_code == 202, r.text
    _wait_status(client, u["b_tok"], job_id, "running")

    r = client.post(f"/api/writing/jobs/{job_id}/stop", headers=_headers(u["b_tok"]))
    assert r.status_code == 200
    _wait_status(client, u["b_tok"], job_id, "stopped")


def test_job_ownership_isolation(client, three_users, writing_env):
    """B 的任务对 C 不可见(404,防存在性泄漏);C 不能确认/停止。"""
    u = three_users
    job_id = _start_stage1(client, u["b_tok"])
    ch = _headers(u["c_tok"])
    assert client.get(f"/api/writing/jobs/{job_id}", headers=ch).status_code == 404
    assert client.get(f"/api/writing/jobs/{job_id}/stream", headers=ch).status_code == 404
    assert client.post(
        f"/api/writing/jobs/{job_id}/confirm", headers=ch,
        json={"confirmed_groups": []},
    ).status_code == 404
    assert client.post(f"/api/writing/jobs/{job_id}/stop", headers=ch).status_code == 404


def test_confirm_rejected_when_not_awaiting(client, three_users, writing_env):
    u = three_users
    job_id = _start_stage1(client, u["b_tok"])
    # 任务尚未到 awaiting_confirm 时确认 → 409(轮询到终态前的窗口)
    status = client.get(f"/api/writing/jobs/{job_id}", headers=_headers(u["b_tok"])).json()["status"]
    if status == "awaiting_confirm":
        pytest.skip("worker 已先到确认点,窗口竞争")
    r = client.post(
        f"/api/writing/jobs/{job_id}/confirm", headers=_headers(u["b_tok"]),
        json={"confirmed_groups": [{"name": "x", "lit_ids": ["lit_x_1"]}]},
    )
    assert r.status_code == 409
