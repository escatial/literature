# -*- coding: utf-8 -*-
"""E2E 全流程冒烟(商业化发布前门禁)。

对运行中的后端跑一次真实 LLM 全流程(小池子),并对每一道质量闸门断言:
  筛选保留 → 归组一次覆盖 → 章节齐全零泄漏 → QA → 摘要 → 交付清单。
用法(后端运行中,需要真实 LLM key):
    set E2E_USERNAME=admin & set E2E_PASSWORD=xxx
    python scripts/e2e_smoke.py --pool-size 12
退出码 0 = 全部断言通过(可发布);非 0 = 任一闸门失败(禁止发布)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import httpx

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")


def _api(method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
    headers = kw.pop("headers", {})
    if token:
        headers["X-Auth-Token"] = token
    return httpx.request(method, f"{BASE}{path}", headers=headers, timeout=30.0, **kw)


def _login(username: str, password: str) -> str:
    r = _api("POST", "/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败: {r.text[:200]}"
    return r.json()["token"]


def _synthetic_pool(n: int, topic: str) -> list[dict]:
    """构造 n 篇可入库的合成文献(主题分两簇,便于归组)。"""
    papers = []
    for i in range(1, n + 1):
        cluster = "品牌" if i % 2 == 0 else "渠道"
        papers.append({
            "lit_id": f"lit_e2e_{i:03d}",
            "source": "user_imported",
            "title": f"{topic}研究{i}:{cluster}视角下的营销策略分析",
            "authors": [f"作者{i}"],
            "journal": "冒烟测试刊",
            "year": 2024,
            "abstract": (
                f"研究{i}:围绕水产品{cluster}主题的营销策略实证分析。"
                f"采用问卷调查与案例方法,样本{i}家,结论支持{cluster}策略优化路径,"
                "对水产行业营销实践具有直接参考价值。"
            ),
        })
    return papers


def _poll_job(token: str, job_id: str, want: set[str], timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        r = _api("GET", f"/api/writing/jobs/{job_id}", token=token)
        assert r.status_code == 200, r.text[:200]
        last = r.json()
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] {last['status']} / {last['phase']}")
        if last["status"] in want:
            return last
        time.sleep(5)
    raise AssertionError(f"等待状态 {want} 超时,最后: {json.dumps(last, ensure_ascii=False)[:200]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="E2E 全流程冒烟(发布前门禁)")
    ap.add_argument("--pool-size", type=int, default=12, help="合成文献池大小")
    ap.add_argument("--topic", default="水产品营销策略冒烟测试")
    ap.add_argument("--stage1-timeout", type=int, default=900)
    ap.add_argument("--stage2-timeout", type=int, default=1800)
    args = ap.parse_args()

    username = os.environ.get("E2E_USERNAME")
    password = os.environ.get("E2E_PASSWORD")
    assert username and password, "需要 E2E_USERNAME / E2E_PASSWORD 环境变量"

    token = _login(username, password)
    print("== 登录 OK ==")

    # ① 文献池入库(独立 task,不污染用户池)
    task_id = f"e2e-smoke-{int(time.time())}"
    papers = _synthetic_pool(args.pool_size, args.topic)
    r = _api("POST", "/api/papers/bulk", token=token,
             json={"papers": papers}, headers={"X-Task-Id": task_id})
    assert r.status_code == 200, f"文献入库失败: {r.text[:200]}"
    print(f"== 文献池 {r.json()['inserted']} 篇入库 OK ==")

    # ② 启动阶段1(筛选+分级+归组)
    r = _api("POST", "/api/writing/jobs", token=token, json={
        "topic": args.topic,
        "papers": papers,
        "classify_mode": "theme",
        "do_screening": True,
    })
    assert r.status_code == 202, f"任务启动失败: {r.text[:200]}"
    job_id = r.json()["job_id"]
    print(f"== 任务 {job_id[:12]} 启动 OK ==")

    # ③ 阶段1 → awaiting_confirm
    st = _poll_job(token, job_id, {"awaiting_confirm"}, args.stage1_timeout)
    job = _api("GET", f"/api/writing/jobs/{job_id}", token=token).json()
    groups = job.get("plan_groups") or []
    assert len(groups) >= 2, f"主题数不足: {len(groups)}"
    print(f"== 阶段1 OK: {len(groups)} 个主题: {[g['name'] for g in groups]} ==")

    # ④ 确认 → 阶段2 → done
    r = _api("POST", f"/api/writing/jobs/{job_id}/confirm", token=token,
             json={"confirmed_groups": groups})
    assert r.status_code == 202, f"确认失败: {r.text[:200]}"
    st = _poll_job(token, job_id, {"done", "failed"}, args.stage2_timeout)
    assert st["status"] == "done", f"阶段2失败: {st.get('error')}"

    # ⑤ 质量单 + 闸门断言
    r = _api("GET", f"/api/writing/jobs/{job_id}/quality", token=token)
    assert r.status_code == 200, r.text[:200]
    q = r.json()

    checks: list[tuple[str, bool, str]] = []
    manifest = q.get("manifest") or {}
    checks.append(("交付零锚点泄漏", manifest.get("anchor_leaks") == 0,
                   f"anchor_leaks={manifest.get('anchor_leaks')}"))
    checks.append(("交付零缺章", not manifest.get("missing_sections"),
                   f"missing={manifest.get('missing_sections')}"))
    secs = q.get("writing_sections") or []
    checks.append(("章节齐全", len(secs) == len(groups),
                   f"sections={len(secs)} expect={len(groups)}"))
    checks.append(("每章有引用", all(s["citations"] for s in secs),
                   str([(s['title'], s['citations']) for s in secs])))
    ga = q.get("grouping", {}).get("attempts") or []
    checks.append(("归组一次全覆盖", bool(ga) and ga[0].get("covered") == args.pool_size,
                   f"attempts={[(a.get('attempt'), a.get('covered')) for a in ga]}"))
    checks.append(("未走自主分组降级", not q.get("grouping", {}).get("fallback_used"),
                   "fallback_used=True"))
    checks.append(("总时长达标(≤15 分钟)",
                   (q.get("total_duration_seconds") or 9999) <= 900,
                   f"{q.get('total_duration_seconds')}s"))

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if failed:
        print(f"\n== E2E 冒烟未通过({len(failed)}/{len(checks)} 项)——禁止发布 ==")
        sys.exit(1)
    print(f"\n== E2E 冒烟通过({len(checks)}/{len(checks)})——可发布 ==")


if __name__ == "__main__":
    main()
