# -*- coding: utf-8 -*-
"""run 质量单 CLI:打印任意一次写作运行的结构化质量数据。

用法(backend 目录下):
    python scripts/quality_report.py --latest          # 最新任务
    python scripts/quality_report.py --job-id fe36...  # 指定任务
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from writing.job_store import build_quality_report  # noqa: E402


def _latest_job_id() -> str:
    conn = sqlite3.connect(str(BACKEND_ROOT / "data" / "lit_review.db"))
    cur = conn.cursor()
    cur.execute("SELECT job_id FROM writing_jobs ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        raise SystemExit("没有任何写作任务")
    return row[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="写作运行质量单")
    ap.add_argument("--job-id", help="指定任务 id")
    ap.add_argument("--latest", action="store_true", help="取最新任务")
    args = ap.parse_args()

    job_id = args.job_id or (_latest_job_id() if args.latest else None)
    if not job_id:
        ap.error("需要 --job-id 或 --latest")

    report = build_quality_report(job_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # 人读摘要
    print("\n===== 摘要 =====")
    print(f"任务: {report['job_id'][:18]}  状态: {report['status']}  "
          f"总时长: {report['total_duration_seconds']}s  事件: {report['total_events']}")
    for st in report.get("stages", []):
        if "seconds" in st:
            print(f"  {st['stage']} → {st['until_event']}: {st['seconds']}s")
    if report.get("screening"):
        print(f"  筛选: 保留 {report['screening']['kept']} 篇 / 剔除 {report['screening']['screened_out']}")
    if report.get("readthrough"):
        rt = report["readthrough"]
        print(f"  通读: {rt['notes']} 条笔记 / {rt['chunks']} 块 / 失败 {rt['failed_chunks']}")
    g = report.get("grouping", {})
    if g.get("attempts"):
        cov = [(a.get("attempt"), a.get("covered"), a.get("papers")) for a in g["attempts"]]
        print(f"  归组尝试(覆盖/池): {cov} | 降级: {g['fallback_used']}")
    if report.get("errors"):
        print(f"  错误: {report['errors']}")
    m = report.get("manifest")
    if m:
        print(f"  交付: 章节 {len(m.get('sections', []))} | 锚点泄漏 {m.get('anchor_leaks')} | "
              f"缺章 {m.get('missing_sections')}")


if __name__ == "__main__":
    main()
