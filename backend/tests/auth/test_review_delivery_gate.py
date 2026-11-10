# -*- coding: utf-8 -*-
"""D1 交付链加固 · POST /reviews 门禁与服务端副本调和(2026-11-01 事故回归)。

场景对照:
- 流中断 → 前端保存锚点中间态 → 无服务端副本时 422 拒绝(此前静默入库);
- 流完成 → 服务端权威落库 → 前端残品 POST 同 task_id → 服务端定稿胜出;
- 前端完整干净副本(同步路径)POST 同 task_id → 覆盖服务端行(章节数更多且零泄漏)。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

PW = "pw-delivery-001"
TASK = "t-delivery-gate-001"

import secrets
import pytest




def _review_payload(sections: list[dict], **extra) -> dict:
    return {
        "topic": "水产品营销策略",
        "classify_mode": "theme",
        "sections": sections,
        "reference_list": "[1] 张三. 某文[J]. 2020.",
        **extra,
    }


def _good_sections() -> list[dict]:
    return [
        {"key": "theme_1", "title": "一、主题", "content": "正文引用[1]。", "citations": []},
    ]


def _anchor_leak_sections() -> list[dict]:
    return [
        {"key": "theme_1", "title": "一、主题", "content": "正文[lit_cn_1]未编号。", "citations": []},
    ]


def _seed_server_review(client, owner_id: str, sections: list[dict]) -> None:
    """直接落库一份服务端权威副本(模拟流完成时的 persist_final)。"""
    import db.models  # noqa: F401
    from db.models import ReviewModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        db.add(ReviewModel(
            topic="水产品营销策略", classify_mode="theme",
            sections=sections, reference_list="[1] 张三. 某文[J]. 2020.",
            screened_out_ids=[], dropped_citations=[],
            owner_user_id=owner_id, task_id=TASK,
            qa_report={"summary": {"overall": "pass", "pass_rate": 1.0}},
            abstract="这是服务端摘要。", keywords=["关键词"],
        ))
        db.commit()


def test_review_post_rejects_anchor_leak(client, three_users):
    """正文残留 [lit_xxx] 且无服务端副本 → 422,残品绝不入库。"""
    u = three_users
    r = client.post(
        "/api/reviews", headers={"X-Auth-Token": u["b_tok"]},
        json=_review_payload(_anchor_leak_sections()),
    )
    assert r.status_code == 422, r.text
    # 未入库
    listed = client.get("/api/reviews", headers={"X-Auth-Token": u["b_tok"]}).json()
    assert listed == []


def test_review_post_rejects_empty_sections(client, three_users):
    u = three_users
    r = client.post(
        "/api/reviews", headers={"X-Auth-Token": u["b_tok"]},
        json=_review_payload([{"key": "theme_1", "title": "一、", "content": "", "citations": []}]),
    )
    assert r.status_code == 422


def test_server_copy_wins_over_partial_client_copy(client, three_users):
    """服务端定稿(2章) vs 前端残品(1章,流中断中间态) → 服务端行保留。"""
    u = three_users
    _seed_server_review(client, u["b_id"], [
        {"key": "theme_1", "title": "一、品牌", "content": "完整第一章[1]。", "citations": []},
        {"key": "theme_2", "title": "二、渠道", "content": "完整第二章[1]。", "citations": []},
    ])
    r = client.post(
        "/api/reviews",
        headers={"X-Auth-Token": u["b_tok"], "X-Task-Id": TASK},
        json=_review_payload(_good_sections()),
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    # 返回的是服务端定稿(2章 + qa_report + abstract)
    assert len(body["sections"]) == 2
    assert body["abstract"] == "这是服务端摘要。"
    assert body["qa_report"]["summary"]["overall"] == "pass"


def test_complete_client_copy_updates_server_row(client, three_users):
    """前端完整干净副本(3章 > 服务端2章,零泄漏) → 覆盖服务端行。"""
    u = three_users
    _seed_server_review(client, u["b_id"], [
        {"key": "theme_1", "title": "一、品牌", "content": "旧稿第一章[1]。", "citations": []},
        {"key": "theme_2", "title": "二、渠道", "content": "旧稿第二章[1]。", "citations": []},
    ])
    better = _good_sections() + [
        {"key": "theme_2", "title": "二、渠道", "content": "新稿第二章[1]。", "citations": []},
        {"key": "comment", "title": "三、文献述评", "content": "述评正文。", "citations": []},
    ]
    r = client.post(
        "/api/reviews",
        headers={"X-Auth-Token": u["b_tok"], "X-Task-Id": TASK},
        json=_review_payload(better, qa_report={"summary": {"overall": "pass", "pass_rate": 1.0}}),
    )
    assert r.status_code in (200, 201), r.text
    assert len(r.json()["sections"]) == 3
    # 库里同 task_id 仍只有一行,内容已更新
    import db.models  # noqa: F401
    from db.models import ReviewModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        rows = db.query(ReviewModel).filter_by(task_id=TASK).all()
        assert len(rows) == 1
        assert len(rows[0].sections) == 3


def test_review_post_accepts_clean_payload(client, three_users):
    """干净副本且无服务端副本 → 正常入库(兼容同步路径)。"""
    u = three_users
    r = client.post(
        "/api/reviews", headers={"X-Auth-Token": u["b_tok"]},
        json=_review_payload(_good_sections()),
    )
    assert r.status_code == 201, r.text
